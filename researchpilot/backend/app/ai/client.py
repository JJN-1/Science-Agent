from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.ai.base import ChatMessage, ChatProvider, ChatRequest, ChatResponse, ProviderError
from app.ai.budget import BudgetExceeded, BudgetManager
from app.ai.degrade import complete_with_degradation
from app.ai.registry import ProviderRegistry
from app.ai.routing import Router
from app.store.dao import runs as runs_dao
from app.store.dao import usage as usage_dao

CACHE_MAX = 256


class LlmGateway:
    """Agent 调用模型的唯一出口：预算 → 缓存 → 路由降级链 → 记账 + 轨迹。"""

    def __init__(self, registry: ProviderRegistry, router: Router,
                 budget: BudgetManager) -> None:
        self.registry = registry
        self.router = router
        self.budget = budget
        self._cache: dict[str, ChatResponse] = {}

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
    ) -> ChatResponse:
        self.budget.check(session, project_id, agent_id, run_id)

        candidates = self.router.candidates(tier)
        first = candidates[0]
        cache_key = self._cache_key(first, messages, schema, tier)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._record(session, project_id=project_id, run_id=run_id,
                         stage_id=stage_id, agent_id=agent_id, tier=tier,
                         response=cached, cost=0.0, cached=True)
            return cached

        request = ChatRequest(
            messages=messages, tier=tier, schema=schema,
            max_tokens=max_tokens, temperature=temperature,
        )
        response = self._call_with_fallback(request, tier)

        provider = self.registry.get(response.provider)
        cost = provider.cost_of(response.prompt_tokens, response.completion_tokens)
        self._cache[cache_key] = response
        if len(self._cache) > CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        self._record(session, project_id=project_id, run_id=run_id,
                     stage_id=stage_id, agent_id=agent_id, tier=tier,
                     response=response, cost=cost, cached=False)
        return response

    def _call_with_fallback(self, request: ChatRequest, tier: str) -> ChatResponse:
        """按档位候选链依次尝试：熔断/失败自动切换降级链（§8.3 / §11.4）。"""
        last_error: ProviderError | None = None
        for cand in self.router.candidates(tier):
            try:
                self.registry.check_available(cand.provider)
                provider = self.registry.get(cand.provider)
                response = complete_with_degradation(provider, request)
            except ProviderError as exc:
                last_error = exc
                self.registry.record_failure(cand.provider)
                continue
            self.registry.record_success(response.provider)
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
