from __future__ import annotations

from dataclasses import dataclass

from app.ai.base import (
    HEALTH_OK,
    ChatProvider,
    CircuitOpen,
    ProviderUnavailable,
    monotonic,
)
from app.ai.providers.mock import MockProvider
from app.ai.providers.openai_compat import OpenAICompatProvider
from app.observability.logging import get_logger

logger = get_logger("ai.registry")

PROVIDER_TYPES: dict[str, type[ChatProvider]] = {
    "mock": MockProvider,
    "openai_compat": OpenAICompatProvider,
}


@dataclass
class CircuitState:
    failures: int = 0
    opened_at: float | None = None


class ProviderRegistry:
    """Provider 注册表：健康元数据 + 熔断器 + 热重载（§8.1 / §8.4 / §11.4）。

    **不按健康状态剔除 provider**（FIX-04）。健康只是元数据，真正的可用性在
    ``check_available()`` / 调用点判定。旧实现遇到未配 Key 的 provider 会静默剔除，
    导致 Router 报「引用了不存在的 provider」——应用启动失败且错误指向错误方向。
    """

    def __init__(self, providers: dict[str, ChatProvider], circuit: dict) -> None:
        self._providers = providers
        self.threshold = int(circuit.get("failure_threshold", 5))
        self.cooldown = float(circuit.get("cooldown_seconds", 300))
        self._circuits: dict[str, CircuitState] = {
            name: CircuitState() for name in providers
        }

    @classmethod
    def from_config(cls, cfg: dict) -> ProviderRegistry:
        ai_cfg = cfg.get("ai", {})
        providers: dict[str, ChatProvider] = {}
        for name, pcfg in (ai_cfg.get("providers") or {}).items():
            ptype = pcfg.get("type")
            factory = PROVIDER_TYPES.get(ptype)
            if factory is None:
                known = ", ".join(sorted(PROVIDER_TYPES))
                raise ValueError(
                    f"未知 provider 类型: {ptype} (provider={name})；已支持: {known}"
                )
            provider = factory(name, pcfg)
            state = provider.health()
            if state != HEALTH_OK:
                # 保留并告警，而不是剔除：配置已写好但 Key 还没录是完全正常的中间状态。
                logger.warning("provider_not_ready", provider=name,
                               health=state, reason=provider.unavailable_reason())
            providers[name] = provider
        return cls(providers, ai_cfg.get("circuit", {}))

    def get(self, name: str) -> ChatProvider:
        if name not in self._providers:
            raise KeyError(f"unknown provider: {name}")
        return self._providers[name]

    def names(self) -> list[str]:
        return sorted(self._providers)

    def providers_map(self) -> dict[str, ChatProvider]:
        return dict(self._providers)

    def health_report(self) -> list[dict]:
        rows = []
        for name in self.names():
            p = self._providers[name]
            st = self._circuits[name]
            state = p.health()  # provider 侧有 TTL 缓存，不会每次真打网络
            rows.append(
                {
                    "name": name,
                    "type": type(p).__name__,
                    "model": p.model,
                    "vendor": p.vendor,
                    "capabilities": sorted(p.capabilities),
                    "health": state,
                    "healthy": state == HEALTH_OK and not self._is_open(st),
                    "circuit_failures": st.failures,
                    "detail": None if state == HEALTH_OK else p.unavailable_reason(),
                }
            )
        return rows

    # ── 熔断器 ──────────────────────────────────
    def _is_open(self, st: CircuitState) -> bool:
        if st.opened_at is None:
            return False
        if monotonic() - st.opened_at >= self.cooldown:
            st.opened_at = None  # 冷却结束，半开放行
            st.failures = self.threshold - 1  # 半开态允许试探一次
            return False
        return True

    def check_available(self, name: str) -> None:
        """调用点可用性判定：健康不达标或处于熔断冷却期都不可用。

        抛出的 ProviderUnavailable 携带可操作提示，降级链会据此切换到下一个候选。
        """
        provider = self.get(name)
        state = provider.health()
        if state != HEALTH_OK:
            raise ProviderUnavailable(provider.unavailable_reason())
        st = self._circuits.get(name)
        if st is not None and self._is_open(st):
            raise CircuitOpen(
                f"provider {name} 处于熔断冷却期（冷却 {self.cooldown:.0f}s）"
            )

    def record_success(self, name: str) -> None:
        st = self._circuits.setdefault(name, CircuitState())
        st.failures = 0
        st.opened_at = None

    def record_failure(self, name: str) -> None:
        st = self._circuits.setdefault(name, CircuitState())
        if st.opened_at is not None:
            return
        st.failures += 1
        if st.failures >= self.threshold:
            st.opened_at = monotonic()

    # ── 热重载（§8.4 全局级）────────────────────
    def reload(self, cfg: dict) -> dict[str, list[str]]:
        """按新配置重建注册表，返回 {removed: [...], added: [...]} 供审计。

        注意：熔断状态目前随重建而重置（FIX-05 待修，需持久化到 app_config）。
        """
        old_names = set(self._providers)
        fresh = ProviderRegistry.from_config(cfg)
        self._providers = fresh._providers
        self._circuits = fresh._circuits
        new_names = set(self._providers)
        return {"removed": sorted(old_names - new_names), "added": sorted(new_names - old_names)}

    def snapshot(self) -> dict[str, str]:
        """冻结当前注册表视图（项目运行期切换只影响新项目）。"""
        return {name: self._providers[name].model for name in self.names()}
