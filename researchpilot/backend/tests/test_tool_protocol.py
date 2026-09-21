"""工具调用协议层（US-409）。

这一层存在的唯一理由是：**在内核之前先把「一次工具调用」表达清楚**。它不执行任何工具，
只负责让 `ChatRequest` / `ChatResponse` 能把工具定义送出去、把模型提出的调用接回来。
四组契约，每一组都对应一个真实的翻车方式：

1. **``arguments`` 是 JSON 字符串**（不是对象）—— 当 dict 用会在第一次真实调用时炸
2. **``_request_with`` 手工重建请求** —— 漏字段不会报错，只会让降级重试少带工具
3. **缓存键必须覆盖 ``tools``** —— 否则不同工具集串缓存，第二次跑拿到第一次的答案
4. **缓存与降级都必须保住 `tool_calls`** —— 丢了它，「模型调了工具」会退化成
   「模型什么都没说」，内核于是静默跳过一整步
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from app.ai.base import (
    HEALTH_OK,
    ChatMessage,
    ChatProvider,
    ChatRequest,
    ChatResponse,
    ToolArgumentsError,
    ToolCall,
    ToolCapabilityMissing,
    parse_tool_arguments,
)
from app.ai.budget import BudgetManager
from app.ai.client import LlmGateway, _deserialize, _serialize
from app.ai.degrade import _request_with, complete_with_degradation
from app.ai.providers.mock import MockProvider
from app.ai.providers.openai_compat import OpenAICompatProvider
from app.ai.registry import ProviderRegistry
from app.ai.routing import RouteCandidate, Router
from app.store.dao import projects as projects_dao
from app.store.dao import runs as runs_dao

TIERS = ("extract", "plan", "critique", "synthesize", "write")


def _tool(name: str, **extra) -> dict:
    """最小可用的工具定义（OpenAI 形状）。"""
    return {
        "type": "function",
        "function": {"name": name, "description": f"{name} 工具",
                     "parameters": {"type": "object", "properties": {}}, **extra},
    }


# ── 1. arguments 是 JSON 字符串 ───────────────────

@pytest.mark.parametrize("raw", [None, "", "   ", "\n"])
def test_absent_arguments_mean_no_arguments(raw):
    """空参数**是合法的**：很多工具就没有入参，不该被判成错误。"""
    assert parse_tool_arguments(raw) == {}


def test_arguments_json_object_parsed():
    assert parse_tool_arguments('{"path": "a.txt", "n": 2}') == {"path": "a.txt", "n": 2}


def test_arguments_tolerates_fences_and_trailing_commas():
    """模型经常裹围栏或带尾随逗号（FIX-06 的容错在这里同样适用）。"""
    assert parse_tool_arguments('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_tool_arguments('{"a": 1,}') == {"a": 1}


def test_malformed_arguments_raise_instead_of_defaulting_to_empty():
    """**绝不静默当空**：把「参数看不懂」当成「没有参数」，工具会带着默认行为跑起来，
    而用户看到的是「执行成功了」—— 它执行的是别的一件事。
    """
    with pytest.raises(ToolArgumentsError) as excinfo:
        parse_tool_arguments("不是 JSON")
    assert excinfo.value.code == "LLM-TOOLS-001"
    assert excinfo.value.raw_output == "不是 JSON"  # 原文要留得住，否则无从排查


def test_arguments_must_be_an_object():
    with pytest.raises(ToolArgumentsError) as excinfo:
        parse_tool_arguments("[1, 2]")
    assert "JSON 对象" in str(excinfo.value)


# ── ToolCall 对象 ────────────────────────────────

def test_tool_call_keeps_arguments_as_a_raw_string():
    """协议层**不解析** arguments，只搬运 —— 解析失败必须在调用点可见。"""
    call = ToolCall(id="c1", name="read_file", arguments='{"path": "a.txt"}')
    assert call.arguments == '{"path": "a.txt"}'
    assert call.parse_arguments() == {"path": "a.txt"}
    assert call.to_dict() == {"id": "c1", "name": "read_file", "arguments": '{"path": "a.txt"}'}


def test_tool_call_from_dict_requires_a_name():
    with pytest.raises(ToolArgumentsError):
        ToolCall.from_dict({"id": "c1", "arguments": "{}"})
    with pytest.raises(ToolArgumentsError):
        ToolCall.from_dict("不是对象")


def test_tool_call_from_dict_normalizes_loose_shapes():
    """id 缺失补空串；arguments 给成对象时按确定键序序列化回字符串。"""
    call = ToolCall.from_dict({"name": "t", "arguments": {"b": 1, "a": 2}})
    assert call.id == ""
    assert call.arguments == '{"a": 2, "b": 1}'
    assert ToolCall.from_dict({"name": "t"}).arguments == ""


# ── 2. _request_with 不丢字段 ────────────────────

def test_request_with_copies_every_field():
    """⚠️ 这条测试的价值在于**未来**：``_request_with`` 手工逐字段重建请求，
    以后给 `ChatRequest` 新增字段而忘了同步时，它会在重试路径上静默丢失
    （`model` 踩过、`tools` 差点再踩一次）。

    用 ``dataclasses.fields`` 枚举而不是手写断言列表：手写的清单会随字段增加而过时，
    而这正是需要它报警的时候。
    """
    original = ChatRequest(
        messages=[ChatMessage(role="user", content="原始")],
        tier="plan",
        schema={"type": "object"},
        max_tokens=42,
        temperature=0.1,
        model="m-x",
        tools=[_tool("read_file")],
        tool_choice="required",
    )
    rebuilt = _request_with(original, [ChatMessage(role="user", content="换过")])

    assert rebuilt.messages[0].content == "换过"
    for spec in dataclasses.fields(ChatRequest):
        if spec.name == "messages":
            continue  # 它本来就是要被换掉的那个参数
        assert getattr(rebuilt, spec.name) == getattr(original, spec.name), spec.name


# ── 3. degrade：能力门与 schema 的交互 ────────────

class _SpyProvider(ChatProvider):
    """记录收到的请求；可按序返回多次不同响应（用于重试路径）。"""

    def __init__(self, capabilities=("json_object",), texts=("ok",), tool_calls=None):
        self.name = "spy"
        self.label = "Spy"
        self.model = "m1"
        self.vendor = "spy"
        self.capabilities = frozenset(capabilities)
        self.price = {"input": 0.0, "output": 0.0}
        self.seen: list[ChatRequest] = []
        self._texts = list(texts)
        self._tool_calls = tool_calls

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.seen.append(request)
        index = min(len(self.seen) - 1, len(self._texts) - 1)
        return ChatResponse(
            text=self._texts[index], provider=self.name, model=self.model,
            tool_calls=self._tool_calls,
        )

    def health(self) -> str:
        return HEALTH_OK


def test_provider_without_tools_capability_is_refused_not_silently_downgraded():
    """`json_object` 缺了可以用 prompt 兜（所以只标降级），`tools` 缺了**兜不住** ——
    没法靠提示词让一个不认识工具协议的端点吐出合规的 tool_calls。
    """
    provider = _SpyProvider(capabilities=("json_object",))
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="hi")], tools=[_tool("read_file")],
    )
    with pytest.raises(ToolCapabilityMissing) as excinfo:
        complete_with_degradation(provider, request)
    assert excinfo.value.code == "LLM-TOOLS-002"
    assert provider.seen == []  # 根本没发出去


def test_tools_pass_through_to_the_provider():
    provider = _SpyProvider(capabilities=("json_object", "tools"))
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="hi")],
        tools=[_tool("read_file")], tool_choice="auto",
    )
    complete_with_degradation(provider, request)

    assert provider.seen[0].tools == [_tool("read_file")]
    assert provider.seen[0].tool_choice == "auto"


def test_provider_without_tools_capability_is_fine_when_no_tools_are_requested():
    """能力门只在**真的带工具**时生效：没带工具的调用不该被它挡住。"""
    provider = _SpyProvider(capabilities=("json_object",))
    response = complete_with_degradation(
        provider, ChatRequest(messages=[ChatMessage(role="user", content="hi")]),
    )
    assert response.text == "ok"


def test_tool_call_turn_skips_schema_validation():
    """模型选择调工具的那一轮，`content` 本就是空的。

    拿空文本去撞结构化校验只会**白白重试**并最终报 `LLM-SCHEMA-001` ——
    而结构化输出的要求属于**最终答案**，不属于中间的工具调用轮。
    """
    call = ToolCall(id="c1", name="read_file", arguments='{"path": "a"}')
    provider = _SpyProvider(capabilities=("json_object", "tools"),
                            texts=("",), tool_calls=[call])
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="hi")],
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"]},
        tools=[_tool("read_file")],
    )
    response = complete_with_degradation(provider, request)

    assert len(provider.seen) == 1, "不该重试"
    assert response.tool_calls == [call]
    assert "schema_retry" not in response.degraded


def test_schema_retry_still_carries_the_tools():
    """降级重试走的是 ``_request_with`` —— 工具定义必须跟着一起去，
    否则重试等于「不带工具再问一遍」，模型自然答「我没法调用工具」。
    """
    provider = _SpyProvider(
        capabilities=("json_object", "tools"), texts=("不是 JSON", '{"ok": true}'),
    )
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="hi")],
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"]},
        tools=[_tool("read_file")], tool_choice="auto",
    )
    response = complete_with_degradation(provider, request)

    assert len(provider.seen) == 2
    assert "schema_retry" in response.degraded
    assert provider.seen[1].tools == [_tool("read_file")]
    assert provider.seen[1].tool_choice == "auto"


# ── 4. openai_compat：请求体 ─────────────────────

def _compat(handler, capabilities=("json_object", "tools")) -> OpenAICompatProvider:
    # api_key_ref 显式留空 = 无需鉴权，测试因此不碰 keyring
    return OpenAICompatProvider(
        "acme",
        {"base_url": "https://acme.test/v1", "models": ["m1"],
         "capabilities": list(capabilities), "api_key_ref": ""},
        transport=httpx.MockTransport(handler),
    )


def _canned(content="hi", tool_calls=None) -> dict:
    message: dict = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"model": "m1", "choices": [{"message": message}], "usage": {}}


def _capture(response_body: dict):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=response_body)

    return captured, handler


def test_compat_request_carries_tools_and_choice():
    captured, handler = _capture(_canned())
    _compat(handler).complete(ChatRequest(
        messages=[ChatMessage(role="user", content="hi")],
        tools=[_tool("read_file")], tool_choice="auto",
    ))

    assert captured["body"]["tools"] == [_tool("read_file")]
    assert captured["body"]["tool_choice"] == "auto"
    assert "response_format" not in captured["body"], "带工具时不该同时要求结构化输出"


def test_compat_omits_tools_when_not_requested():
    captured, handler = _capture(_canned())
    _compat(handler).complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    assert "tools" not in captured["body"]
    assert "tool_choice" not in captured["body"]


def test_compat_replays_tool_linkage_in_messages():
    """回放历史时两条规则缺一不可：``tool`` 消息带 ``tool_call_id``，
    带工具调用的 ``assistant`` 消息把 ``tool_calls`` 一起带上。
    少任何一条，端点会直接拒收整条请求。
    """
    captured, handler = _capture(_canned())
    call = ToolCall(id="c1", name="read_file", arguments='{"path": "a"}')
    _compat(handler).complete(ChatRequest(messages=[
        ChatMessage(role="user", content="读一下 a"),
        ChatMessage(role="assistant", content="", tool_calls=[call]),
        ChatMessage(role="tool", content="文件内容", tool_call_id="c1"),
    ]))

    messages = captured["body"]["messages"]
    assert messages[1]["tool_calls"] == [{
        "id": "c1", "type": "function",
        "function": {"name": "read_file", "arguments": '{"path": "a"}'},
    }]
    assert messages[2]["tool_call_id"] == "c1"
    assert messages[0].get("tool_call_id") is None


# ── 5. openai_compat：响应解析 ───────────────────

def test_compat_parses_tool_calls_and_keeps_arguments_verbatim():
    raw_arguments = '{"path": "a.txt", "n": 2}'
    _, handler = _capture(_canned(content=None, tool_calls=[{
        "id": "call_abc", "type": "function",
        "function": {"name": "read_file", "arguments": raw_arguments},
    }]))
    response = _compat(handler).complete(
        ChatRequest(messages=[ChatMessage(role="user", content="hi")]),
    )

    assert response.text == "", "content 为 null 时必须是空串而不是 None"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "call_abc"
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == raw_arguments  # 原样，不修不解析
    assert response.tool_calls[0].parse_arguments() == {"path": "a.txt", "n": 2}


def test_compat_synthesizes_a_deterministic_id_when_upstream_omits_it():
    """上游偶尔不给 id。补一个**确定性**的（按位置）—— 结果回传要靠它对齐，
    随机 id 会让同一份脚本两次跑出的轨迹无法逐项比较（G2 第 7 条）。
    """
    _, handler = _capture(_canned(tool_calls=[
        {"type": "function", "function": {"name": "a", "arguments": "{}"}},
        {"type": "function", "function": {"name": "b", "arguments": "{}"}},
    ]))
    response = _compat(handler).complete(
        ChatRequest(messages=[ChatMessage(role="user", content="hi")]),
    )
    assert [c.id for c in response.tool_calls] == ["call_0", "call_1"]


def test_compat_ignores_malformed_tool_call_entries():
    """缺 name 的条目直接跳过：它无法被执行，留着只会把错误推到更远的地方。"""
    _, handler = _capture(_canned(tool_calls=[
        {"id": "x", "type": "function", "function": {"arguments": "{}"}},
        {"id": "y", "type": "function", "function": {"name": "good", "arguments": "{}"}},
        "不是对象",
    ]))
    response = _compat(handler).complete(
        ChatRequest(messages=[ChatMessage(role="user", content="hi")]),
    )
    assert [c.name for c in response.tool_calls] == ["good"]


def test_compat_reports_no_tool_calls_as_none():
    _, handler = _capture(_canned(content="直接作答"))
    response = _compat(handler).complete(
        ChatRequest(messages=[ChatMessage(role="user", content="hi")]),
    )
    assert response.tool_calls is None


# ── 6. mock：确定性工具调用 ──────────────────────

def _scripted(**cfg) -> MockProvider:
    base = {"models": ["m1"], "vendor": "mock",
            "capabilities": ["json_object", "tools"]}
    return MockProvider("m", {**base, **cfg})


def test_mock_emits_scripted_calls_then_stops_with_text():
    """脚本用尽后回到普通文本 —— 一次会话会**自己结束**，
    内核测试不必依赖「模型永远调工具」这种不真实的假设。
    """
    provider = _scripted(tool_script=[
        {"name": "read_file", "arguments": {"path": "a"}},
        {"name": "write_file", "arguments": {"path": "b", "text": "hi"}},
    ], response="做完了")
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="hi")], tools=[_tool("read_file")],
    )

    first = provider.complete(request)
    second = provider.complete(request)
    third = provider.complete(request)

    assert [c.name for c in first.tool_calls] == ["read_file"]
    assert [c.id for c in first.tool_calls] == ["call_1"]
    assert second.tool_calls[0].name == "write_file"
    assert second.tool_calls[0].id == "call_2"
    assert second.tool_calls[0].arguments == '{"path": "b", "text": "hi"}'
    assert third.tool_calls is None
    assert third.text == "做完了"


def test_mock_needs_tools_in_the_request_to_emit_calls():
    """不带工具的请求不该凭空冒出工具调用 —— 那会让「工具没下发」这类问题
    在内核里看起来像正常运行。
    """
    provider = _scripted(tool_script=[{"name": "read_file"}])
    response = provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert response.tool_calls is None


def test_mock_script_is_fully_deterministic():
    """同一份脚本两次运行 → 逐项相同（参数键序也要稳定，否则比对会被无关差异绊倒）。"""
    def run() -> tuple:
        provider = _scripted(tool_script=[{"name": "t", "arguments": {"b": 1, "a": 2}}])
        response = provider.complete(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")], tools=[_tool("t")],
        ))
        return tuple(c.to_dict() for c in response.tool_calls)

    assert run() == run()
    assert run()[0]["arguments"] == '{"a": 2, "b": 1}'


def test_mock_rejects_a_script_entry_without_a_name():
    provider = _scripted(tool_script=[{"arguments": {}}])
    with pytest.raises(Exception) as excinfo:
        provider.complete(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")], tools=[_tool("t")],
        ))
    assert "tool_script" in str(excinfo.value)


# ── 7. 缓存：键覆盖 tools，且保住 tool_calls ──────

def _gateway(cfg: dict) -> tuple[LlmGateway, ProviderRegistry, Router]:
    registry = ProviderRegistry.from_config(cfg)
    router = Router.from_config(cfg, registry.providers_map())
    return LlmGateway(registry, router, BudgetManager(cfg["ai"]["budget"])), registry, router


def _cfg(providers: dict, routing: dict | None = None) -> dict:
    return {"ai": {
        "providers": providers,
        "routing": routing or {t: [{"provider": next(iter(providers)), "model": "m1"}]
                               for t in TIERS},
        "budget": {"project_total": 10.0},
    }}


def test_cache_key_covers_tools_and_tool_choice():
    """不同工具集 = 不同的选择空间 = 不同答案。不进键就会串缓存，
    而串缓存看起来完全正常 —— 这才是它难被发现的原因。
    """
    cfg = _cfg({"a": {"type": "mock", "models": ["m1"], "vendor": "mock"}})
    gateway, _, _ = _gateway(cfg)
    cand = RouteCandidate("a", "m1")
    messages = [ChatMessage(role="user", content="hi")]

    key_none = gateway._cache_key(cand, messages, None, "plan", None)
    key_a = gateway._cache_key(cand, messages, None, "plan", [_tool("a")])
    key_b = gateway._cache_key(cand, messages, None, "plan", [_tool("b")])
    key_auto = gateway._cache_key(cand, messages, None, "plan", [_tool("a")], "auto")
    key_required = gateway._cache_key(cand, messages, None, "plan", [_tool("a")], "required")

    assert len({key_none, key_a, key_b, key_auto, key_required}) == 5
    # 同一输入必须稳定
    assert gateway._cache_key(cand, messages, None, "plan", [_tool("a")]) == key_a
    # 工具**描述**改了也要换键（所以放完整结构而不是自定摘要）
    assert gateway._cache_key(
        cand, messages, None, "plan", [_tool("a", description="改过的描述")],
    ) != key_a


def test_cached_response_keeps_tool_calls(session):
    """缓存命中把「模型调了工具」变成「模型什么都没说」——内核据此会静默跳过一整步。
    这类差异只在**第二次**跑同一输入时出现，最难被发现。
    """
    cfg = _cfg({"a": {"type": "mock", "models": ["m1"], "vendor": "mock",
                      "capabilities": ["json_object", "tools"],
                      "tool_script": [{"name": "read_file", "arguments": {"path": "a"}}]}})
    gateway, _, _ = _gateway(cfg)
    project = projects_dao.create(session, title="缓存", domain="cs-ai")
    run = runs_dao.create_run(session, project_id=project.id, stage_id="S1", agent_id="scout")
    kwargs = dict(session=session, project_id=project.id, run_id=run.id, stage_id="S1",
                  agent_id="scout", tier="plan",
                  messages=[ChatMessage(role="user", content="读一下 a")],
                  tools=[_tool("read_file")])

    first = gateway.call(**kwargs)
    second = gateway.call(**kwargs)

    assert [c.name for c in first.tool_calls] == ["read_file"]
    assert [c.name for c in second.tool_calls] == ["read_file"]

    steps = [s for s in runs_dao.list_steps(session, run.id) if s.kind == "llm_call"]
    assert [s.content["cached"] for s in steps] == [False, True]
    assert steps[1].content["tool_calls"][0]["name"] == "read_file"


def test_different_tool_sets_do_not_share_a_cache_entry(session):
    """端到端确认：带 A 工具的会话不能把答案喂给带 B 工具的会话。"""
    cfg = _cfg({"a": {"type": "mock", "models": ["m1"], "vendor": "mock",
                      "capabilities": ["json_object", "tools"],
                      "tool_script": [{"name": "read_file"}, {"name": "write_file"}]}})
    gateway, _, _ = _gateway(cfg)
    project = projects_dao.create(session, title="串缓存", domain="cs-ai")
    run = runs_dao.create_run(session, project_id=project.id, stage_id="S1", agent_id="scout")
    common = dict(session=session, project_id=project.id, run_id=run.id, stage_id="S1",
                  agent_id="scout", tier="plan",
                  messages=[ChatMessage(role="user", content="同样的话")])

    with_read = gateway.call(**common, tools=[_tool("read_file")])
    with_write = gateway.call(**common, tools=[_tool("write_file")])

    assert [c.name for c in with_read.tool_calls] == ["read_file"]
    assert [c.name for c in with_write.tool_calls] == ["write_file"], "说明第二问没命中第一问的缓存"


def test_cache_payload_roundtrip_preserves_tool_calls():
    """序列化是缓存与轨迹的公共出口，单独钉一下。"""
    call = ToolCall(id="c1", name="read_file", arguments='{"path": "a"}')
    original = ChatResponse(text="", provider="a", model="m1", tool_calls=[call])

    restored = _deserialize(_serialize(original))

    assert restored.tool_calls == [call]
    assert _serialize(ChatResponse(text="x", provider="a", model="m1"))["tool_calls"] == []
    assert _deserialize(_serialize(ChatResponse(text="x", provider="a", model="m1"))).tool_calls is None


# ── 8. 能力不匹配不进熔断 ────────────────────────

def test_missing_tools_capability_skips_candidate_without_counting_a_failure(session):
    """能力不匹配是**配置问题**，不是后端故障。

    若把它记成失败，一个「没配 tools 的后端被带工具的档位引用」会在 5 次调用后
    打开它的熔断 —— 于是它在**别的档位**上也变成不可用，故障横向扩散到无关调用。
    """
    cfg = _cfg(
        {"notools": {"type": "mock", "models": ["m1"], "vendor": "mock",
                     "capabilities": ["json_object"], "response": "no"},
         "hastools": {"type": "mock", "models": ["m2"], "vendor": "mock",
                      "capabilities": ["json_object", "tools"],
                      "tool_script": [{"name": "read_file"}]}},
        routing={t: [{"provider": "notools", "model": "m1"},
                     {"provider": "hastools", "model": "m2"}] for t in TIERS},
    )
    gateway, registry, _ = _gateway(cfg)
    project = projects_dao.create(session, title="能力门", domain="cs-ai")
    run = runs_dao.create_run(session, project_id=project.id, stage_id="S1", agent_id="scout")

    response = gateway.call(
        session, project_id=project.id, run_id=run.id, stage_id="S1", agent_id="scout",
        tier="plan", messages=[ChatMessage(role="user", content="hi")],
        tools=[_tool("read_file")],
    )

    assert response.provider == "hastools", "应降级到下一个候选"
    assert [c.name for c in response.tool_calls] == ["read_file"]

    by_id = {row["id"]: row for row in registry.health_report()}
    assert by_id["notools"]["circuit_failures"] == 0, "能力不匹配不该计入熔断"
