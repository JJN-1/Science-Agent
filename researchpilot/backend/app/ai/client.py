from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace

from sqlalchemy.orm import Session

from app.ai.base import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ProviderError,
    ToolCall,
    ToolCapabilityMissing,
    monotonic,
)
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
        "attempts": response.attempts,
        "degraded": list(response.degraded),
        # ⚠️ 必须带上：漏了它，缓存命中会把「模型调用了工具」变成「模型什么都没说」，
        # 内核据此认为该轮无需执行工具，于是静默跳过一整步（缓存命中恰恰是最难
        # 被发现的一类差异 —— 它只在第二次跑同一输入时才出现）。
        "tool_calls": [c.to_dict() for c in (response.tool_calls or [])],
    }


def _deserialize(payload: dict) -> ChatResponse:
    """还原缓存响应；latency_ms 归零——本次没有发生网络往返。"""
    raw_calls = payload.get("tool_calls") or []
    return ChatResponse(
        text=payload["text"],
        provider=payload["provider"],
        model=payload["model"],
        prompt_tokens=int(payload.get("prompt_tokens", 0)),
        completion_tokens=int(payload.get("completion_tokens", 0)),
        latency_ms=0,
        degraded=list(payload.get("degraded") or []),
        attempts=int(payload.get("attempts", 1)),
        tool_calls=[ToolCall(**call) for call in raw_calls] or None,
    )


class LlmGateway:
    """Agent 调用模型的唯一出口：预算 → 缓存 → 路由降级链 → 记账 + 轨迹。

    缓存落在 ``llm_cache`` 表（FIX-05），跨重启有效；键由
    (provider, model, tier, messages, schema) 规范化哈希得到。

    ``on_step`` 是给异步作业层用的结算钩子（FIX-03）：``_record`` 落完轨迹与
    记账后回调一次，让「模型返回了」这件事能变成一条可订阅的事件。

    ``on_start`` 是**发起前**的钩子：模型调用是全链路最慢的一步，等待期间必须有
    进度信号，否则界面与作业流是一片死寂（真实事故：「等了好几分钟，什么都看不到」）。
    只在真的要发网络请求时才回调 —— 缓存命中没有等待可言。
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
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        seed: int | None = None,
        on_step: Callable[[ChatResponse, float, bool], None] | None = None,
        on_start: Callable[[str, list[dict]], None] | None = None,
    ) -> ChatResponse:
        self.budget.check(session, project_id, agent_id, run_id)

        cached = self._lookup_cache(session, tier, messages, schema, tools, tool_choice, seed)
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
            tools=tools, tool_choice=tool_choice, seed=seed,
        )
        if on_start is not None:
            # 把整条候选项链报出去：用户看到的是「在等谁、还有几个备选」，
            # 而不是「卡住了」。降级链是设计的一部分（§8.3），等待时也该可见。
            on_start(tier, [{"provider": c.provider, "model": c.model}
                            for c in self.router.candidates(tier)])
        started = monotonic()
        try:
            response = self._call_with_fallback(request, tier, session)
        except ProviderError as exc:
            self._record_failure(
                session, project_id=project_id, run_id=run_id, stage_id=stage_id,
                agent_id=agent_id, tier=tier, exc=exc,
                latency_ms=int((monotonic() - started) * 1000),
            )
            raise

        provider = self.registry.get(response.provider)
        cost = provider.cost_of(response.prompt_tokens, response.completion_tokens)
        # 键按**实际响应方**生成：降级到 B 的结果记在 B 名下，不污染 A 的键（FIX-05 / D4）
        if self.cache_enabled:
            llm_cache_dao.put(
                session,
                cache_key=self._cache_key(
                    RouteCandidate(response.provider, response.model),
                    messages, schema, tier, tools, tool_choice,
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
                      schema: dict | None, tools: list[dict] | None = None,
                      tool_choice: str | dict | None = None,
                      seed: int | None = None) -> ChatResponse | None:
        """按候选链顺序逐个试命中，在第一个「当前可用」的候选处停下（D4）。

        停下的理由：候选链的顺序就是偏好顺序。若首候选 A 已恢复可用，就该走 A——
        不能因为降级期间在 B 名下留过缓存，就永远拿 B 的旧答案把 A 挡住。
        反过来，不可用的候选本来就轮不到它作答，它的缓存命中仍然有效。
        """
        if not self.cache_enabled:
            return None
        for cand in self.router.candidates(tier):
            key = self._cache_key(cand, messages, schema, tier, tools, tool_choice, seed)
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
        """按档位候选链依次尝试：熔断/失败自动切换降级链（§8.3 / §11.4）。

        每个候选都按自己的 ``(provider, model)`` 发请求 —— 档位里选的模型必须真正
        生效（旧实现只用了 ``provider.models[0]``，路由表里的 ``model`` 是死配置）。
        """
        last_error: ProviderError | None = None
        for cand in self.router.candidates(tier):
            try:
                self.registry.check_available(cand.provider, session)
                provider = self.registry.get(cand.provider)
                response = complete_with_degradation(
                    provider, replace(request, model=cand.model or None)
                )
            except ToolCapabilityMissing as exc:
                # 能力不匹配是**配置问题**，不是后端故障：只跳过，不记失败。
                # 若在这里 record_failure，一个「没配 tools 的后端被带工具的档位引用」
                # 会在若干次调用后把它的熔断打开，连累它在**别的档位**上也变不可用。
                last_error = exc
                exc.provider = exc.provider or cand.provider
                exc.model = exc.model or cand.model
                continue
            except ProviderError as exc:
                last_error = exc
                # 补上归属：provider 自己不知道被谁调度，但失败记账必须落到具体后端
                exc.provider = exc.provider or cand.provider
                exc.model = exc.model or cand.model
                self.registry.record_failure(cand.provider, session)
                continue
            self.registry.record_success(response.provider, session)
            return response
        raise last_error or ProviderError("LLM-PROVIDER-001: 无可用模型后端")

    def _cache_key(self, cand, messages: list[ChatMessage], schema: dict | None,
                   tier: str, tools: list[dict] | None = None,
                   tool_choice: str | dict | None = None,
                   seed: int | None = None) -> str:
        """(provider, model, tier, messages, schema, tools, tool_choice, seed) 的规范化哈希。

        ``tools`` / ``tool_choice`` **必须进键**：同一段对话带不同的工具集，模型的
        选择空间完全不同，答案自然不同。不进键的话「先跑了带 A 工具的会话、再跑
        带 B 工具的会话」会命中同一条缓存 —— 后者拿到的是前者的答案，
        而且看起来完全正常（这类串缓存最难被发现）。

        ``seed`` 同理进键：它的用途就是把采样钉在一条确定路径上，两个不同的 seed
        是两次不同的请求，共用键会让「确定性模式」拿到一次非确定性调用的缓存。

        这里放**完整的** ``tools`` 而不是「摘要」：摘要要自己定义归一化规则，
        规则一旦漏掉某个字段（例如函数描述的改动）就会碰撞；而它最终是被 sha256
        吃掉的，体积不构成理由 —— 唯一的要求是确定性，``sort_keys`` 已经保证。
        """
        raw = json.dumps(
            {
                "p": cand.provider, "m": cand.model, "t": tier,
                "msgs": [
                    [
                        m.role, m.content, m.tool_call_id,
                        [[c.id, c.name, c.arguments] for c in (m.tool_calls or [])],
                    ]
                    for m in messages
                ],
                "s": schema,
                "tools": tools,
                "tc": tool_choice,
                "seed": seed,
            },
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
            attempts=response.attempts,
        )
        runs_dao.add_step(session, run_id=run_id, kind="llm_call", content={
            "text": response.text,
            "provider": response.provider,
            "model": response.model,
            "tier": tier,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "cost": round(cost, 6),
            "latency_ms": response.latency_ms,
            "attempts": response.attempts,
            "cached": cached,
            "degraded": response.degraded,
            # 记下来，事后才答得出「这一轮模型到底调没调工具、调的什么」——
            # 只记 text 的话，调工具的那一轮在轨迹里就是一条空文本。
            "tool_calls": [c.to_dict() for c in (response.tool_calls or [])],
        })

    def _record_failure(self, session: Session, *, project_id: int, run_id: int,
                        stage_id: str, agent_id: str, tier: str,
                        exc: ProviderError, latency_ms: int) -> None:
        """失败的调用同样落一行记账。

        只记成功的话，用户看到的「供应商统计」永远是残缺的：那次跑挂了的请求根本
        不存在，于是「有没有发出去」「发给了谁」「等了多久」全都答不上来（真实投诉）。
        失败行 ``cost=0``，不参与花费合计，只参与调用次数与失败计数 —— 钱没花出去，
        但事情确实发生过，这两件事都得如实记录。
        """
        usage_dao.record(
            session, stage_id=stage_id, agent_id=agent_id,
            provider=exc.provider or "unknown", model=exc.model or "unknown", tier=tier,
            cost=0.0, latency_ms=latency_ms, cached=False,
            project_id=project_id, run_id=run_id,
            status=usage_dao.STATUS_FAILED, error=str(exc), attempts=exc.attempts,
        )
