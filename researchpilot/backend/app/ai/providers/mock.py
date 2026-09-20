from __future__ import annotations

import time

from app.ai.base import (
    HEALTH_DOWN,
    HEALTH_OK,
    ChatProvider,
    ChatRequest,
    ChatResponse,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
    resolve_models,
)


class MockProvider(ChatProvider):
    """确定性假模型：驱动测试与零配置首启，可配置失败与延迟。"""

    def __init__(self, name: str, cfg: dict) -> None:
        self.name = name
        self.label = str(cfg.get("name") or name).strip() or name
        self.models = resolve_models(name, cfg)
        self.model = self.models[0]
        self.vendor = cfg.get("vendor", "mock")
        self.capabilities = frozenset(cfg.get("capabilities", ["json_object"]))
        self.price = cfg.get("price") or cfg.get("price_per_1k") or {"input": 0.0, "output": 0.0}
        self._healthy = cfg.get("healthy", True)
        self._fail_times = int(cfg.get("fail_times", 0))
        self._fail_with = cfg.get("fail_with", "unavailable")  # unavailable|rate_limited|quota
        # latency_ms 只是响应里的元数据；delay_ms 是真睡。
        # 要验证「长任务全程有进度」得靠后者——否则任务一瞬间就结束了，
        # 流式进度无从观察（Sprint 3 的 US-311/US-304 前端验收都依赖它）。
        self._latency_ms = int(cfg.get("latency_ms", 0))
        self._delay_ms = int(cfg.get("delay_ms", 0))
        self._response = cfg.get("response", '{"items": []}')
        self._calls = 0

    def complete(self, request: ChatRequest) -> ChatResponse:
        self._calls += 1
        if self._delay_ms > 0:
            time.sleep(self._delay_ms / 1000)
        if self._calls <= self._fail_times:
            if self._fail_with == "rate_limited":
                raise RateLimited(f"{self.name} rate limited")
            if self._fail_with == "quota":
                raise QuotaExceeded(f"{self.name} quota exceeded")
            raise ProviderUnavailable(f"{self.name} unavailable")
        return ChatResponse(
            text=self._response,
            provider=self.name,
            model=request.model or self.model,
            prompt_tokens=len(" ".join(m.content for m in request.messages)) // 4,
            completion_tokens=len(self._response) // 4,
            latency_ms=self._latency_ms,
        )

    def health(self) -> str:
        return HEALTH_OK if self._healthy else HEALTH_DOWN

    def unavailable_reason(self) -> str:
        return f"mock provider {self.name} 被配置为不可用（healthy=false）"
