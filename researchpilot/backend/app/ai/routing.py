from __future__ import annotations

from dataclasses import dataclass

from app.ai.base import ChatProvider
from app.observability.logging import get_logger

logger = get_logger("ai.routing")

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

    def __post_init__(self) -> None:
        """构造即规范化：两侧一律去空白。

        候选来自三处 —— 配置文件、`PATCH /routing` 的表单、测试。任何一处漏了
        trim，脏值就会顺着 ``provider_switch_log`` 与 ``.as_config()`` 一路扩散，
        最后在调用点变成一个「模型不存在」的 404，而日志里查不出是谁写进去的。
        """
        self.provider = str(self.provider or "").strip()
        self.model = str(self.model or "").strip()


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
            cls._validate_tier(tier, candidates, providers, strict_models=False)
            routes[tier] = candidates
        missing = [t for t in TIERS if t not in routes]
        if missing:
            raise RoutingError(f"档位未配置路由: {', '.join(missing)}")
        cls._validate_vendor_separation(routes, providers)
        return cls(routes)

    @classmethod
    def _validate_tier(
        cls, tier: str, candidates: list[RouteCandidate],
        providers: dict[str, ChatProvider], *, strict_models: bool = False,
    ) -> None:
        if not candidates:
            raise RoutingError(f"档位 {tier} 路由为空")
        for cand in candidates:
            if not cand.model:
                raise RoutingError(f"档位 {tier} 的候选缺少模型 ID")
            provider = providers.get(cand.provider)
            if provider is None:
                raise RoutingError(
                    f"档位 {tier} 引用了不存在的 provider: {cand.provider}"
                )
            cls._check_model_declared(tier, cand, provider, strict=strict_models)
            required = TIER_REQUIRED_CAPS.get(tier, frozenset())
            lack = required - provider.capabilities
            if lack:
                raise RoutingError(
                    f"档位 {tier} 的 provider {cand.provider} 缺少能力: {', '.join(sorted(lack))}"
                )

    @classmethod
    def _check_model_declared(
        cls, tier: str, cand: RouteCandidate, provider: ChatProvider, *, strict: bool
    ) -> None:
        """模型 ID 必须在该后端声明的 ``models`` 里。

        判据以**本地声明**为准（设计 §8.1：清单与能力只认本地配置，不认上游）。
        声明为空时放弃这道检查 —— 探测常常拿不到清单，不该因此把用户锁死；
        mock 后端同样跳过（见下）。

        两条路径刻意不同：

        - **改路由（strict，`PATCH /routing`）**：这是我们唯一能当场纠正用户的时刻，
          直接拒掉并告诉它声明了哪些模型。从前写错的模型要等到真发起调用才发现，
          而且表现成「后端不可用」，用户根本看不出是自己填错了。路由的模型输入框
          已改成可自由输入，这道检查正是让自由输入安全的那张网。
        - **加载配置（非 strict）**：配置漂移（例如重新探测后 models 收窄）不该让
          应用起不来。打告警，真到调用时再按失败处理 —— 硬失败会让用户连界面都进不去，
          也就没地方改回来。
        """
        declared = [str(m) for m in (getattr(provider, "models", None) or [])]
        if not declared or cand.model in declared:
            return
        if getattr(provider, "vendor", "") == MOCK_VENDOR:
            # mock 是开发替身：任何模型名都返回同一段内置响应，它的清单不具约束力。
            # 与 `_validate_vendor_separation` 用同一个豁免判据，保持一套口径。
            logger.debug("routing_model_check_skipped_for_mock",
                         tier=tier, provider=cand.provider, model=cand.model)
            return
        detail = (
            f"档位 {tier} 的 provider {cand.provider} 未声明模型 {cand.model!r}；"
            f"它声明了: {', '.join(declared)}"
        )
        if strict:
            raise RoutingError(detail)
        logger.warning("routing_model_not_declared", tier=tier,
                       provider=cand.provider, model=cand.model,
                       declared=declared)

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
        self._validate_tier(tier, candidates, providers, strict_models=True)
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
