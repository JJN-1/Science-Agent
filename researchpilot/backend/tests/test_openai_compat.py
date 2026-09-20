from __future__ import annotations

import httpx
import pytest

from app.ai.base import (
    HEALTH_DOWN,
    HEALTH_OK,
    HEALTH_UNCONFIGURED,
    ChatMessage,
    ChatRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from app.ai.degrade import complete_with_degradation
from app.ai.providers.openai_compat import OpenAICompatProvider

SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}}

# 贴近真实 S1 的形状：`questions` 必需且非空，每项要有 question
S1_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        }
    },
    "required": ["questions"],
}


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    """默认注入可用 Key；个别测试自行覆盖。"""
    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.get_password", lambda svc, ref: "sk-test"
    )


def _provider(handler, **cfg_over) -> OpenAICompatProvider:
    cfg = {
        "base_url": "https://api.example.com/v1",
        "model": "test-model",
        "vendor": "acme",
        "backoff_base": 0.0,
    }
    cfg.update(cfg_over)
    return OpenAICompatProvider("acme", cfg, transport=httpx.MockTransport(handler))


def _ok(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_complete_success_and_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok("hello"))

    p = _provider(handler)
    resp = p.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert resp.text == "hello"
    assert (resp.prompt_tokens, resp.completion_tokens) == (10, 5)
    assert resp.provider == "acme"
    assert resp.attempts == 1


def test_latency_is_measured_and_attempts_counted():
    """记账里全是 latency_ms=0 时，「等了几分钟」和「只要 200ms」在数据上无法区分。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(200, json=_ok("ok"))

    p = _provider(handler)
    resp = p.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert resp.attempts == 2  # 重试一次才成功
    assert resp.latency_ms > 0


def test_http_error_carries_body_and_attempts():
    """只有状态码没有响应体的日志判不出原因（真实事故：403 后面其实是 FreeTierError）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="FreeTierError: no balance")

    p = _provider(handler)
    with pytest.raises(ProviderError) as excinfo:
        p.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert excinfo.value.raw_output == "FreeTierError: no balance"
    assert excinfo.value.attempts == 1


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.get_password", lambda *a: None
    )
    p = _provider(lambda req: httpx.Response(200, json=_ok("x")))
    with pytest.raises(ProviderUnavailable, match="未配置 API Key"):
        p.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))


def test_key_from_keyring(monkeypatch):
    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.get_password", lambda svc, ref: "sk-test"
    )
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_ok("x"))

    p = _provider(handler)
    p.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert seen["auth"] == "Bearer sk-test"


def test_retry_on_500_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_ok("ok"))

    p = _provider(handler)
    resp = p.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert resp.text == "ok"
    assert calls["n"] == 2


def test_no_retry_on_400():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    p = _provider(handler)
    with pytest.raises(ProviderError, match="HTTP 400"):
        p.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert calls["n"] == 1


def test_429_exhausted_raises_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    p = _provider(handler, max_retries=1)
    with pytest.raises(RateLimited):
        p.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))


def test_response_format_only_with_capability():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, json=_ok("{}"))

    p = _provider(handler)  # 默认含 json_object
    p.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")], schema=SCHEMA))
    assert b"response_format" in seen["body"]

    p2 = _provider(handler, capabilities=[])
    p2.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")], schema=SCHEMA))
    assert b"response_format" not in seen["body"]


def test_health_ok_and_no_key(monkeypatch):
    """FIX-04：健康三态——「没录 Key」是 unconfigured，不是 down。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.get_password", lambda svc, ref: "sk"
    )
    assert _provider(handler).health() == HEALTH_OK

    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.get_password", lambda svc, ref: None
    )
    p = _provider(handler)
    assert p.health() == HEALTH_UNCONFIGURED
    assert "API Key" in p.unavailable_reason()  # 提示要能直接告诉用户怎么修


def test_health_down_on_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    p = _provider(handler)
    assert p.health() == HEALTH_DOWN
    assert p.unavailable_reason()


def test_health_down_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    p = _provider(handler)
    assert p.health() == HEALTH_DOWN


def test_health_probe_cached_and_invalidated(monkeypatch):
    """health() 每次真打网络会让设置页卡住数秒，因此结果要短期缓存。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.get_password", lambda svc, ref: "sk"
    )
    p = _provider(handler)
    assert p.health() == HEALTH_OK
    assert p.health() == HEALTH_OK
    assert calls["n"] == 1  # 第二次命中缓存
    p.invalidate_health()
    p.health()
    assert calls["n"] == 2


# ── 降级链（US-202）────────────────────────────

def test_degrade_schema_injection_for_missing_json_object():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["body"] = _json.loads(request.read())
        return httpx.Response(200, json=_ok('{"items": [1]}'))

    p = _provider(handler, capabilities=[])
    resp = complete_with_degradation(
        p, ChatRequest(messages=[ChatMessage(role="user", content="hi")], schema=SCHEMA)
    )
    assert resp.degraded == ["schema_prompt"]
    system = seen["body"]["messages"][0]
    assert system["role"] == "system"
    assert "JSON Schema" in system["content"]


def test_degrade_injects_schema_even_with_json_object_capability():
    """止血回归：声明了 ``json_object`` 的后端**同样**必须知道输出形状。

    ``response_format=json_object`` 只保证「是 JSON」，不保证形状。旧实现据此跳过
    schema 注入，于是模型只被告知「输出 JSON」却不知道要什么结构 —— 回一个
    ``{"response": "……", "format": "JSON"}`` 是合法 JSON，就被当成成功吃掉了。
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["body"] = _json.loads(request.read())
        return httpx.Response(200, json=_ok('{"items": [1]}'))

    p = _provider(handler)  # 默认含 json_object
    resp = complete_with_degradation(
        p, ChatRequest(messages=[ChatMessage(role="user", content="hi")], schema=SCHEMA)
    )
    assert resp.degraded == []  # 能力齐全 → 不该出现任何降级标记
    system = seen["body"]["messages"][0]
    assert system["role"] == "system"
    assert "JSON Schema" in system["content"]


def test_degrade_merges_schema_into_existing_system_message():
    """阶段本来就带 system prompt：schema 要并进去，不能造出两条 system 消息。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["body"] = _json.loads(request.read())
        return httpx.Response(200, json=_ok('{"items": [1]}'))

    p = _provider(handler)
    complete_with_degradation(p, ChatRequest(
        messages=[ChatMessage(role="system", content="你是选题专家"),
                  ChatMessage(role="user", content="hi")],
        schema=SCHEMA,
    ))
    messages = seen["body"]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "你是选题专家" in messages[0]["content"]
    assert "JSON Schema" in messages[0]["content"]


def test_degrade_shape_violation_retries_with_path_then_keeps_raw():
    """「合法 JSON 但形状不对」必须重试并把**具体违规点**写进重试指令，最终失败。

    只说「不是合法 JSON」会让模型困惑（它觉得自己给的确实是 JSON）而原地打转；
    指出 ``$ 缺少必需字段 questions`` 才是可修复的指令。
    """
    calls: list[dict] = []
    wrong = '{"response": "我已生成 3 个候选研究问题", "format": "JSON"}'

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        calls.append(_json.loads(request.read()))
        return httpx.Response(200, json=_ok(wrong))

    p = _provider(handler)
    with pytest.raises(ProviderError) as excinfo:
        complete_with_degradation(
            p, ChatRequest(messages=[ChatMessage(role="user", content="hi")], schema=S1_SCHEMA)
        )

    assert len(calls) == 2  # 形状违规 → 重试一次
    retry_user = calls[1]["messages"][-1]["content"]
    assert "缺少必需字段 questions" in retry_user
    assert "LLM-SCHEMA-001" in str(excinfo.value)
    assert excinfo.value.raw_output == wrong  # 原文挂在异常上，供轨迹落盘（FIX-06）


def test_degrade_retry_with_error_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        content = "not json" if calls["n"] == 1 else '{"items": []}'
        return httpx.Response(200, json=_ok(content))

    p = _provider(handler, capabilities=[])
    resp = complete_with_degradation(
        p, ChatRequest(messages=[ChatMessage(role="user", content="hi")], schema=SCHEMA)
    )
    assert resp.degraded == ["schema_prompt", "schema_retry"]
    assert calls["n"] == 2
    assert resp.text == '{"items": []}'


def test_degrade_accepts_fenced_json_without_retry():
    """FIX-06：带 ```json 围栏是正常输出，不该被判为校验失败而触发重试。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_ok('```json\n{"items": [1]}\n```'))

    p = _provider(handler, capabilities=[])
    resp = complete_with_degradation(
        p, ChatRequest(messages=[ChatMessage(role="user", content="hi")], schema=SCHEMA)
    )
    assert calls["n"] == 1
    assert "schema_retry" not in resp.degraded


def test_degrade_double_failure_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok("still not json"))

    p = _provider(handler, capabilities=[])
    with pytest.raises(ProviderError, match="LLM-SCHEMA-001"):
        complete_with_degradation(
            p, ChatRequest(messages=[ChatMessage(role="user", content="hi")], schema=SCHEMA)
        )


def test_no_schema_passes_through():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok("plain text"))

    p = _provider(handler)
    resp = complete_with_degradation(
        p, ChatRequest(messages=[ChatMessage(role="user", content="hi")])
    )
    assert resp.text == "plain text"
    assert resp.degraded == []
