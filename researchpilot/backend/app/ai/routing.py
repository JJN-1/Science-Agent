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
        """critique 档厂商必须与 plan/synthesize 不同，否则交叉验证失效（§8.2）。

        mock 供开发与测试，不受此约束。
        """

        def vendor(tier: str) -> str | None:
            chain = routes.get(tier) or []
            if not chain:
                return None
            p = providers.get(chain[0].provider)
            return p.vendor if p else None

        critique_vendor = vendor("critique")
        if critique_vendor == "mock":
            return
        for tier in ("plan", "synthesize"):
            if critique_vendor is not None and critique_vendor == vendor(tier):
                raise RoutingError(
                    f"critique 档厂商 ({critique_vendor}) 不得与 {tier} 档相同（交叉验证约束）"
                )

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
