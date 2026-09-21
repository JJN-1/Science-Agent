from __future__ import annotations

import json
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
    ToolCall,
    resolve_models,
)


class MockProvider(ChatProvider):
    """确定性假模型：驱动测试与零配置首启，可配置失败与延迟。

    ``tool_script`` 让内核循环可以**离线且可复现**地跑起来（US-409）：

        {"tool_script": [
            {"name": "read_file", "arguments": {"path": "a.txt"}},
            {"name": "write_file", "arguments": {"path": "b.txt", "text": "hi"}},
        ]}

    第 N 次调用吐出脚本第 N 条工具调用，脚本用尽后回到普通文本作答 ——
    这样一次会话会**自己结束**，内核测试不必依赖「模型永远调工具」这种不真实的假设。
    脚本里的工具名应取自本次请求下发的 ``tools``；mock 不做这层校验，
    「调了没下发的工具」属于内核要处置的错误，该在内核测试里覆盖。
    """

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
        self._tool_script = list(cfg.get("tool_script") or [])
        self._calls = 0

    def _scripted_tool_calls(self, request: ChatRequest) -> list[ToolCall] | None:
        """按脚本产出**确定性**工具调用；脚本用尽或本次不带工具 → ``None``。

        调用 id 由序号生成（``call_1``、``call_2``…）而不是随机串：内核的检查点与
        轨迹要靠它对齐「结果回传给了哪次调用」，随机会让同一份脚本两次跑出的
        轨迹无法逐项比较（G2 第 7 条）。
        """
        if not request.tools or not self._tool_script:
            return None
        index = self._calls - 1
        if index >= len(self._tool_script):
            return None
        spec = self._tool_script[index] or {}
        name = str(spec.get("name") or "").strip()
        if not name:
            raise ProviderUnavailable(f"mock provider {self.name} 的 tool_script 第 {index + 1} 条缺少 name")
        arguments = spec.get("arguments")
        if arguments is None:
            arguments = "{}"
        elif not isinstance(arguments, str):
            # sort_keys：同一份脚本必须每次都序列化成同一个字符串，
            # 否则「两次运行结果相同」的断言会被键序这种无关差异绊倒。
            arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        return [ToolCall(id=f"call_{self._calls}", name=name, arguments=arguments)]

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
        tool_calls = self._scripted_tool_calls(request)
        text = "" if tool_calls else self._response
        return ChatResponse(
            text=text,
            provider=self.name,
            model=request.model or self.model,
            prompt_tokens=len(" ".join(m.content for m in request.messages)) // 4,
            completion_tokens=len(text) // 4,
            latency_ms=self._latency_ms,
            tool_calls=tool_calls,
        )

    def health(self) -> str:
        return HEALTH_OK if self._healthy else HEALTH_DOWN

    def unavailable_reason(self) -> str:
        return f"mock provider {self.name} 被配置为不可用（healthy=false）"
