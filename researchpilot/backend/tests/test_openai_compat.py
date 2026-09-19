from __future__ import annotations

import httpx
import pytest

from app.ai.base import ChatMessage, ChatRequest, ProviderError, ProviderUnavailable, RateLimited
from app.ai.degrade import complete_with_degradation
from app.ai.providers.openai_compat import OpenAICompatProvider

SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}}


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
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.get_password", lambda svc, ref: "sk"
    )
    assert _provider(handler).health() is True
    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.get_password", lambda svc, ref: None
    )
    assert _provider(handler).health() is False


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
