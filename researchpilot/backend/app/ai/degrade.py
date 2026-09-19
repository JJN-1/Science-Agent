from __future__ import annotations

import json

from app.ai.base import (
    ChatMessage,
    ChatProvider,
    ChatRequest,
    ChatResponse,
    ProviderError,
)


def _system_prompt_with_schema(schema: dict) -> str:
    return (
        "你必须输出一个符合以下 JSON Schema 的 JSON 对象，不要输出任何其他内容：\n"
        + json.dumps(schema, ensure_ascii=False)
    )


def _try_parse(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except (ValueError, TypeError):
        return False


def complete_with_degradation(provider: ChatProvider, request: ChatRequest) -> ChatResponse:
    """能力降级包装（§8.3）：

    - 缺 JSON Schema 强约束 → schema 注入 system prompt（降级标记 schema_prompt）
    - 结构化输出校验失败 → 携带错误信息重试一次（降级标记 schema_retry）
    - 重试仍失败 → LLM-SCHEMA-001
    """
    degraded: list[str] = []
    messages = list(request.messages)

    if request.schema and "json_object" not in provider.capabilities:
        degraded.append("schema_prompt")
        messages = [ChatMessage(role="system", content=_system_prompt_with_schema(request.schema))]
        messages += request.messages

    effective = ChatRequest(
        messages=messages, tier=request.tier, schema=request.schema,
        max_tokens=request.max_tokens, temperature=request.temperature,
    )
    response = provider.complete(effective)

    if request.schema and not _try_parse(response.text):
        retry_messages = messages + [
            ChatMessage(role="assistant", content=response.text),
            ChatMessage(
                role="user",
                content="上面的输出不是合法 JSON。请重新输出，只输出符合 schema 的 JSON 对象。",
            ),
        ]
        degraded.append("schema_retry")
        retry_request = ChatRequest(
            messages=retry_messages, tier=request.tier, schema=request.schema,
            max_tokens=request.max_tokens, temperature=request.temperature,
        )
        response = provider.complete(retry_request)
        if not _try_parse(response.text):
            raise ProviderError("LLM-SCHEMA-001: 结构化输出两次校验失败")

    response.degraded = degraded + response.degraded
    return response
