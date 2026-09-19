from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.ai.base import ChatMessage, ChatRequest, ChatResponse, ProviderError
from app.ai.budget import BudgetManager
from app.ai.degrade import complete_with_degradation
from app.ai.registry import ProviderRegistry
from app.ai.routing import RouteCandidate, Router
from app.store.dao import llm_cache as llm_cache_dao
from app.store.dao import runs as runs_dao
from app.store.dao import usage as usage_dao

DEFAULT_CACHE_TTL_SECONDS = 604800  # 7 天


def _serialize(response: ChatResponse) -> dict:
    return {
        "text": response.text,
        "provider": response.provider,
        "model": response.model,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "latency_ms": response.latency_ms,
        "degraded": list(response.degraded),
    }


def _deserialize(payload: dict) -> ChatResponse:
    """还原缓存响应；latency_ms 归零——本次没有发生网络往返。"""
    return ChatResponse(
        text=payload["text"],
        provider=payload["provider"],
        model=payload["model"],
        prompt_tokens=int(payload.get("prompt_tokens", 0)),
        completion_tokens=int(payload.get("completion_tokens", 0)),
        latency_ms=0,
        degraded=list(payload.get("degraded") or []),
    )


class LlmGateway:
    """Agent 调用模型的唯一出口：预算 → 缓存 → 路由降级链 → 记账 + 轨迹。

    缓存落在 ``llm_cache`` 表（FIX-05），跨重启有效；键由
    (provider, model, tier, messages, schema) 规范化哈希得到。

    ``on_step`` 是给异步作业层用的结算钩子（FIX-03）：``_record`` 落完轨迹与
    记账后回调一次，让「模型返回了」这件事能变成一条可订阅的事件。
    """

    def __init__(self, registry: ProviderRegistry, router: Router,
                 budget: BudgetManager, cache_cfg: dict | None = None) -> None:
        self.registry = registry
        self.router = router
        self.budget = budget
        cfg = cache_cfg or {}
        self.cache_enabled = bool(cfg.get("enabled", True))
        self.cache_ttl = int(cfg.get("ttl_seconds", DEFAULT_CACHE_TTL_SECONDS))

    def call(
        self,
        session: Session,
        *,
        project_id: int,
        run_id: int,
        stage_id: str,
        agent_id: str,
        tier: str,
        messages: list[ChatMessage],
        schema: dict | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        on_step: Callable[[ChatResponse, float, bool], None] | None = None,
    ) -> ChatResponse:
        self.budget.check(session, project_id, agent_id, run_id)

        cached = self._lookup_cache(session, tier, messages, schema)
        if cached is not None:
            self._record(session, project_id=project_id, run_id=run_id,
                         stage_id=stage_id, agent_id=agent_id, tier=tier,
                         response=cached, cost=0.0, cached=True)
            if on_step is not None:
                on_step(cached, 0.0, True)
            return cached

        request = ChatRequest(
            messages=messages, tier=tier, schema=schema,
            max_tokens=max_tokens, temperature=temperature,
        )
        response = self._call_with_fallback(request, tier, session)

        provider = self.registry.get(response.provider)
        cost = provider.cost_of(response.prompt_tokens, response.completion_tokens)
        # 键按**实际响应方**生成：降级到 B 的结果记在 B 名下，不污染 A 的键（FIX-05 / D4）
        if self.cache_enabled:
            llm_cache_dao.put(
                session,
                cache_key=self._cache_key(
                    RouteCandidate(response.provider, response.model),
                    messages, schema, tier,
                ),
                provider=response.provider,
                model=response.model,
                tier=tier,
                response=_serialize(response),
                ttl_seconds=self.cache_ttl,
            )
        self._record(session, project_id=project_id, run_id=run_id,
                     stage_id=stage_id, agent_id=agent_id, tier=tier,
                     response=response, cost=cost, cached=False)
        if on_step is not None:
            on_step(response, cost, False)
        return response

    def _lookup_cache(self, session: Session, tier: str, messages: list[ChatMessage],
                      schema: dict | None) -> ChatResponse | None:
        """按候选链顺序逐个试命中，在第一个「当前可用」的候选处停下（D4）。

        停下的理由：候选链的顺序就是偏好顺序。若首候选 A 已恢复可用，就该走 A——
        不能因为降级期间在 B 名下留过缓存，就永远拿 B 的旧答案把 A 挡住。
        反过来，不可用的候选本来就轮不到它作答，它的缓存命中仍然有效。
        """
        if not self.cache_enabled:
            return None
        for cand in self.router.candidates(tier):
            key = self._cache_key(cand, messages, schema, tier)
            row = llm_cache_dao.get(session, key)
            if row is not None:
                return _deserialize(row.response)
            try:
                self.registry.check_available(cand.provider, session)
            except ProviderError:
                continue  # 该候选本就会被降级链跳过，继续往后看
            return None  # 第一个可用候选未命中 → 交给真实调用
        return None

    def _call_with_fallback(self, request: ChatRequest, tier: str,
                            session: Session | None = None) -> ChatResponse:
        """按档位候选链依次尝试：熔断/失败自动切换降级链（§8.3 / §11.4）。"""
        last_error: ProviderError | None = None
        for cand in self.router.candidates(tier):
            try:
                self.registry.check_available(cand.provider, session)
                provider = self.registry.get(cand.provider)
                response = complete_with_degradation(provider, request)
            except ProviderError as exc:
                last_error = exc
                self.registry.record_failure(cand.provider, session)
                continue
            self.registry.record_success(response.provider, session)
            return response
        raise last_error or ProviderError("LLM-PROVIDER-001: 无可用模型后端")

    def _cache_key(self, cand, messages: list[ChatMessage], schema: dict | None,
                   tier: str) -> str:
        raw = json.dumps(
            {"p": cand.provider, "m": cand.model, "t": tier,
             "msgs": [[m.role, m.content] for m in messages], "s": schema},
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _record(self, session: Session, *, project_id: int, run_id: int,
                stage_id: str, agent_id: str, tier: str, response: ChatResponse,
                cost: float, cached: bool) -> None:
        usage_dao.record(
            session, stage_id=stage_id, agent_id=agent_id,
            provider=response.provider, model=response.model, tier=tier,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            cost=cost, latency_ms=response.latency_ms, cached=cached,
            degraded=response.degraded, project_id=project_id, run_id=run_id,
        )
        runs_dao.add_step(session, run_id=run_id, kind="llm_call", content={
            "text": response.text,
            "provider": response.provider,
            "model": response.model,
            "tier": tier,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "cost": round(cost, 6),
            "cached": cached,
            "degraded": response.degraded,
        })
