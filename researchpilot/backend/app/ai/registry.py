from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.ai.base import (
    HEALTH_OK,
    ChatProvider,
    CircuitOpen,
    ProviderUnavailable,
    wall_clock,
)
from app.ai.providers.mock import MockProvider
from app.ai.providers.openai_compat import OpenAICompatProvider
from app.observability.logging import get_logger
from app.store.dao import app_config as app_config_dao

logger = get_logger("ai.registry")

PROVIDER_TYPES: dict[str, type[ChatProvider]] = {
    "mock": MockProvider,
    "openai_compat": OpenAICompatProvider,
}

# 熔断状态在 app_config 里的键前缀（FIX-05）
CIRCUIT_KEY_PREFIX = "circuit:"


@dataclass
class CircuitState:
    failures: int = 0
    opened_at: float | None = None  # wall-clock 秒（跨进程可比，见 base.wall_clock）


class ProviderRegistry:
    """Provider 注册表：健康元数据 + 熔断器 + 热重载（§8.1 / §8.4 / §11.4）。

    **不按健康状态剔除 provider**（FIX-04）。健康只是元数据，真正的可用性在
    ``check_available()`` / 调用点判定。旧实现遇到未配 Key 的 provider 会静默剔除，
    导致 Router 报「引用了不存在的 provider」——应用启动失败且错误指向错误方向。

    **熔断状态落库**（FIX-05）。此前只在内存里，重启即清零，被熔断的后端会在重启后
    立刻再挨一遍失败。现在通过 ``app_config`` 的 ``circuit:<provider>`` 键持久化：
    调用点把 session 传进来即可顺带落库，不传则只改内存（保持可单测、可脱库使用）。
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
                    "healthy": state == HEALTH_OK and not self._is_open(name),
                    "circuit_failures": st.failures,
                    "detail": None if state == HEALTH_OK else p.unavailable_reason(),
                }
            )
        return rows

    # ── 熔断器（FIX-05：状态落库）──────────────────
    def load_circuits(self, session: Session) -> None:
        """从 app_config 回读熔断状态（启动时调用一次）。

        只覆盖已存在的键；库里没有记录的 provider 保持初始的干净状态。
        """
        stored = app_config_dao.get_prefix(session, CIRCUIT_KEY_PREFIX)
        restored = 0
        for name, raw in stored.items():
            if name not in self._providers:
                # provider 已从配置里移除，顺带清掉它的残留状态
                app_config_dao.delete(session, f"{CIRCUIT_KEY_PREFIX}{name}")
                continue
            st = self._circuits.setdefault(name, CircuitState())
            st.failures = int(raw.get("failures", 0))
            opened_at = raw.get("opened_at")
            st.opened_at = float(opened_at) if opened_at is not None else None
            if st.failures or st.opened_at is not None:
                restored += 1
        if restored:
            logger.info("circuit_state_restored", providers=restored)

    def _persist(self, name: str, session: Session | None) -> None:
        """把单个 provider 的熔断状态写回 app_config；干净状态则删键。"""
        if session is None:
            return
        st = self._circuits.get(name)
        if st is None:
            return
        key = f"{CIRCUIT_KEY_PREFIX}{name}"
        if st.failures == 0 and st.opened_at is None:
            app_config_dao.delete(session, key)
            return
        app_config_dao.put(session, key, {
            "failures": st.failures,
            "opened_at": st.opened_at,
        })

    def _refresh(self, name: str, session: Session | None = None) -> None:
        """冷却到期则转半开：清 opened_at、把计数降到阈值-1（放行一次试探）。

        这个跃迁本身是状态变更，同样要落库，否则重启后又会回到「刚熔断」。
        """
        st = self._circuits.get(name)
        if st is None or st.opened_at is None:
            return
        if wall_clock() - st.opened_at >= self.cooldown:
            st.opened_at = None
            st.failures = self.threshold - 1
            self._persist(name, session)

    def _is_open(self, name: str, session: Session | None = None) -> bool:
        self._refresh(name, session)
        st = self._circuits.get(name)
        return st is not None and st.opened_at is not None

    def check_available(self, name: str, session: Session | None = None) -> None:
        """调用点可用性判定：健康不达标或处于熔断冷却期都不可用。

        抛出的 ProviderUnavailable 携带可操作提示，降级链会据此切换到下一个候选。
        """
        provider = self.get(name)
        state = provider.health()
        if state != HEALTH_OK:
            raise ProviderUnavailable(provider.unavailable_reason())
        if self._is_open(name, session):
            raise CircuitOpen(
                f"provider {name} 处于熔断冷却期（冷却 {self.cooldown:.0f}s）"
            )

    def record_success(self, name: str, session: Session | None = None) -> None:
        st = self._circuits.setdefault(name, CircuitState())
        st.failures = 0
        st.opened_at = None
        self._persist(name, session)

    def record_failure(self, name: str, session: Session | None = None) -> None:
        st = self._circuits.setdefault(name, CircuitState())
        if st.opened_at is not None:
            return
        st.failures += 1
        if st.failures >= self.threshold:
            st.opened_at = wall_clock()
        self._persist(name, session)

    # ── 热重载（§8.4 全局级）────────────────────
    def reload(self, cfg: dict, session: Session | None = None) -> dict[str, list[str]]:
        """按新配置重建注册表，返回 {removed: [...], added: [...]} 供审计。

        熔断状态**不再随重建清零**（FIX-05）：留下的 provider 保留各自状态，
        新 provider 从干净状态开始，被移除的 provider 连持久化记录一起清掉。
        """
        old_names = set(self._providers)
        fresh = ProviderRegistry.from_config(cfg)
        new_names = set(fresh._providers)

        self._providers = fresh._providers
        self.threshold = fresh.threshold
        self.cooldown = fresh.cooldown

        kept = {n: st for n, st in self._circuits.items() if n in new_names}
        for name in new_names:
            kept.setdefault(name, CircuitState())
        self._circuits = kept

        removed = sorted(old_names - new_names)
        if session is not None:
            # 被移除的 provider 连持久化记录一起清掉（它已不在 _circuits 里，
            # 不能走 _persist）
            for name in removed:
                app_config_dao.delete(session, f"{CIRCUIT_KEY_PREFIX}{name}")
            for name in sorted(new_names):
                self._persist(name, session)
        return {"removed": removed, "added": sorted(new_names - old_names)}

    def snapshot(self) -> dict[str, str]:
        """冻结当前注册表视图（项目运行期切换只影响新项目）。"""
        return {name: self._providers[name].model for name in self.names()}
