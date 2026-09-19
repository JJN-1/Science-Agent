from __future__ import annotations

from dataclasses import dataclass

from app.ai.base import CircuitOpen, ChatProvider, monotonic
from app.ai.providers.mock import MockProvider

PROVIDER_TYPES: dict[str, type[ChatProvider]] = {
    "mock": MockProvider,
    # openai_compat 在 US-202 加入
}


@dataclass
class CircuitState:
    failures: int = 0
    opened_at: float | None = None


class ProviderRegistry:
    """Provider 注册表：健康过滤 + 熔断器 + 热重载（§8.1 / §8.4 / §11.4）。"""

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
                raise ValueError(f"未知 provider 类型: {ptype} (provider={name})")
            provider = factory(name, pcfg)
            if not provider.health():
                continue  # 不健康的后端不进入注册表
            providers[name] = provider
        return cls(providers, ai_cfg.get("circuit", {}))

    def get(self, name: str) -> ChatProvider:
        if name not in self._providers:
            raise KeyError(f"unknown provider: {name}")
        return self._providers[name]

    def names(self) -> list[str]:
        return sorted(self._providers)

    def health_report(self) -> list[dict]:
        rows = []
        for name in self.names():
            p = self._providers[name]
            st = self._circuits[name]
            healthy = p.health() and not self._is_open(st)
            rows.append(
                {
                    "name": name,
                    "type": type(p).__name__,
                    "model": p.model,
                    "vendor": p.vendor,
                    "capabilities": sorted(p.capabilities),
                    "healthy": healthy,
                    "circuit_failures": st.failures,
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
        st = self._circuits.get(name)
        if st is not None and self._is_open(st):
            raise CircuitOpen(f"provider {name} 处于熔断冷却期")

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
    def reload(self, cfg: dict) -> dict[str, str]:
        """按新配置重建注册表，返回 {旧: 新} 的名称映射供审计。"""
        old_names = set(self._providers)
        fresh = ProviderRegistry.from_config(cfg)
        self._providers = fresh._providers
        self._circuits = fresh._circuits
        new_names = set(self._providers)
        return {"removed": sorted(old_names - new_names), "added": sorted(new_names - old_names)}

    def snapshot(self) -> dict[str, str]:
        """冻结当前注册表视图（项目运行期切换只影响新项目）。"""
        return {name: self._providers[name].model for name in self.names()}
