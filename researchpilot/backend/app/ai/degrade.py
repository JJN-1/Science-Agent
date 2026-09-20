from __future__ import annotations

from app.ai.base import (
    ChatMessage,
    ChatProvider,
    ChatRequest,
    ChatResponse,
    ProviderError,
)
from app.ai.json_utils import extract_json
from app.ai.schema_utils import schema_errors, schema_json


def _system_prompt_with_schema(schema: dict) -> str:
    return (
        "你必须输出一个符合以下 JSON Schema 的 JSON 对象，不要输出任何其他内容：\n"
        + schema_json(schema)
    )


def _retry_hint(errors: list[str]) -> str:
    """携带**具体违规点**的重试指令。

    旧版本只说「不是合法 JSON」，模型面对一个「合法但形状不对」的输出时会困惑
    （它觉得自己给的确实是 JSON），于是原地打转。把违规路径摆出来才可修复。
    """
    detail = "；".join(errors[:5])
    return (
        f"上面的输出不符合 schema：{detail}。"
        "请重新输出**只包含**符合 schema 的 JSON 对象，不要添加解释文字、不要包裹其他字段。"
    )


def _validation_errors(text: str, schema: dict) -> list[str]:
    """把「解析失败」与「形状不符」统一成同一种错误表示。"""
    try:
        parsed = extract_json(text)
    except ValueError as exc:
        return [f"不是合法 JSON（{exc}）"]
    return schema_errors(parsed, schema)


def complete_with_degradation(provider: ChatProvider, request: ChatRequest) -> ChatResponse:
    """能力降级包装（§8.3 + 形状校验）：

    - **schema 始终下发**：`response_format=json_object` 只保证「输出是 JSON」，
      不保证形状；形状只能靠 prompt 讲清楚。旧实现只在 provider 缺 `json_object`
      能力时才注入 schema，于是「声明了 json_object 的后端」反而完全不知道要输出什么
      结构，返回 `{"response": "..."}` 之类的合法 JSON 就被当成成功吃掉了。
    - 缺 JSON 强约束能力 → 额外标记降级 `schema_prompt`（描述 provider 的能力缺口，
      与上面的「始终下发」是两件事）
    - 形状校验失败 → 携带违规点重试一次（降级标记 `schema_retry`）
    - 重试仍失败 → `LLM-SCHEMA-001`
    """
    degraded: list[str] = []
    messages = list(request.messages)

    if request.schema:
        schema_text = _system_prompt_with_schema(request.schema)
        if messages and messages[0].role == "system":
            # 阶段本来就带 system prompt：合并进去，不要造出两条 system 消息
            # （部分端点对多 system 消息的处理不一致）
            messages[0] = ChatMessage("system", f"{messages[0].content}\n\n{schema_text}")
        else:
            messages = [ChatMessage(role="system", content=schema_text)] + messages
        if "json_object" not in provider.capabilities:
            degraded.append("schema_prompt")

    response = provider.complete(_request_with(request, messages))

    if not request.schema:
        response.degraded = degraded + response.degraded
        return response

    errors = _validation_errors(response.text, request.schema)
    if not errors:
        response.degraded = degraded + response.degraded
        return response

    degraded.append("schema_retry")
    retry_messages = messages + [
        ChatMessage(role="assistant", content=response.text),
        ChatMessage(role="user", content=_retry_hint(errors)),
    ]
    response = provider.complete(_request_with(request, retry_messages))

    errors = _validation_errors(response.text, request.schema)
    if errors:
        raise ProviderError(
            "LLM-SCHEMA-001: 结构化输出两次校验失败；"
            f"违规点: {'；'.join(errors[:5])}；原始输出片段: {response.text[:200]!r}",
            raw_output=response.text,
        )
    response.degraded = degraded + response.degraded
    return response


def _request_with(request: ChatRequest, messages: list[ChatMessage]) -> ChatRequest:
    return ChatRequest(
        messages=messages, tier=request.tier, schema=request.schema,
        max_tokens=request.max_tokens, temperature=request.temperature,
    )
