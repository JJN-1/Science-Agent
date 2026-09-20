from __future__ import annotations

from dataclasses import dataclass

from app.ai.base import ChatProvider

TIERS = ("extract", "plan", "critique", "synthesize", "write")

# 各档位对 provider 能力的最低要求（§8.2：结论类档位必须结构化输出）
TIER_REQUIRED_CAPS: dict[str, frozenset[str]] = {
    "extract": frozenset(),
    "plan": frozenset({"json_object"}),
    "critique": frozenset({"json_object"}),
    "synthesize": frozenset({"json_object"}),
    "write": frozenset(),
}


class RoutingError(ValueError):
    code = "LLM-ROUTE-001"


@dataclass
class RouteCandidate:
    provider: str
    model: str


MOCK_VENDOR = "mock"


class Router:
    """档位路由：tier → 有序候选链（首个可用，其余为降级链）。"""

    def __init__(self, routes: dict[str, list[RouteCandidate]]) -> None:
        self._routes = routes

    @classmethod
    def from_config(
        cls, cfg: dict, providers: dict[str, ChatProvider]
    ) -> Router:
        routes_raw = (cfg.get("ai", {}) or {}).get("routing") or {}
        routes: dict[str, list[RouteCandidate]] = {}
        for tier, chain in routes_raw.items():
            if tier not in TIERS:
                raise RoutingError(f"未知档位: {tier}")
            candidates = [
                RouteCandidate(provider=item["provider"], model=item["model"])
                for item in chain
            ]
            cls._validate_tier(tier, candidates, providers)
            routes[tier] = candidates
        missing = [t for t in TIERS if t not in routes]
        if missing:
            raise RoutingError(f"档位未配置路由: {', '.join(missing)}")
        cls._validate_vendor_separation(routes, providers)
        return cls(routes)

    @classmethod
    def _validate_tier(
        cls, tier: str, candidates: list[RouteCandidate],
        providers: dict[str, ChatProvider],
    ) -> None:
        if not candidates:
            raise RoutingError(f"档位 {tier} 路由为空")
        for cand in candidates:
            provider = providers.get(cand.provider)
            if provider is None:
                raise RoutingError(
                    f"档位 {tier} 引用了不存在的 provider: {cand.provider}"
                )
            required = TIER_REQUIRED_CAPS.get(tier, frozenset())
            lack = required - provider.capabilities
            if lack:
                raise RoutingError(
                    f"档位 {tier} 的 provider {cand.provider} 缺少能力: {', '.join(sorted(lack))}"
                )

    @classmethod
    def _validate_vendor_separation(
        cls, routes: dict[str, list[RouteCandidate]],
        providers: dict[str, ChatProvider],
    ) -> None:
        """critique 档必须留着至少一个与 plan / synthesize 都不相同的候选（§8.2）。

        要害是**别让同一件事自己审自己**。判定用的是 ``(vendor, model)`` 这个
        「同一个后端的同一个模型」，不是 vendor 本身 —— **同厂商换个模型是允许的**
        （用户明确要求：同一个供应商的大小模型搭配评审既省事又不失独立性）。

        并且要看**整条候选链**：只看首候选会漏掉「首候选换了、降级之后又撞在一起」
        的情况——那时 critique 会静默退化成自我评审。

        vendor 为 mock 的一方一律豁免：mock 只用于开发与测试。
        """

        critique = cls._identities(routes.get("critique") or [], providers)
        if not critique:
            return
        for tier in ("plan", "synthesize"):
            other = cls._identities(routes.get(tier) or [], providers)
            if not other:
                continue
            if critique <= other:
                raise RoutingError(
                    f"critique 档的候选全部与 {tier} 档相同（同一个后端的同一个模型"
                    f"既产出又被审），交叉验证失效；换一个模型即可"
                )

    @classmethod
    def _identities(
        cls, chain: list[RouteCandidate], providers: dict[str, ChatProvider]
    ) -> set[tuple[str, str]]:
        """候选链的 ``(厂商, 模型)`` 集合 —— 「是不是同一个后端同一个模型」的判据。

        整条链都用 mock 时返回空集（不参与比较）：mock 只供开发与测试。
        """
        out: set[tuple[str, str]] = set()
        for cand in chain:
            provider = providers.get(cand.provider)
            if provider is None:
                continue
            if provider.vendor == MOCK_VENDOR:
                return set()
            out.add((provider.vendor, cand.model))
        return out

    def candidates(self, tier: str) -> list[RouteCandidate]:
        chain = self._routes.get(tier)
        if not chain:
            raise RoutingError(f"档位 {tier} 未配置路由")
        return list(chain)

    def update_tier(
        self, tier: str, candidates: list[RouteCandidate],
        providers: dict[str, ChatProvider],
    ) -> None:
        """档位级热切换（§8.4），带完整校验。"""
        if tier not in TIERS:
            raise RoutingError(f"未知档位: {tier}")
        self._validate_tier(tier, candidates, providers)
        new_routes = dict(self._routes)
        new_routes[tier] = candidates
        self._validate_vendor_separation(new_routes, providers)
        self._routes = new_routes

    def as_config(self) -> dict:
        return {
            tier: [{"provider": c.provider, "model": c.model} for c in chain]
            for tier, chain in self._routes.items()
        }

    def replace(self, cfg: dict, providers: dict[str, ChatProvider]) -> None:
        """按配置整体替换路由表，**原地更新**（US-312）。

        `LlmGateway` 在构造时就持有了 Router 引用，替换实例会让它继续用旧路由；
        原地改 `_routes` 才能保证「改配置即生效」。
        """
        self._routes = Router.from_config(cfg, providers)._routes
