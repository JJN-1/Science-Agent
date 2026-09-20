"""US-312：用户自定义模型接入的端到端回归。

覆盖三件事：
1. 接入表单写进去的配置真能被装配成 provider 并生效（无需重启）；
2. 校验与引用保护到位——非法地址/未知能力被拒，被路由引用的 provider 不许删；
3. 免鉴权端点（api_key_ref 留空）在**保存与探测两条路径**上语义一致，
   自定义头可用，模型探测失败时给手工填写提示。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.ai.base import HEALTH_DOWN, ChatMessage, ChatRequest
from app.ai.provider_config import ProviderConfigError, normalize, normalize_base_url
from app.ai.providers.openai_compat import OpenAICompatProvider
from app.main import create_app

# 端口 9 是 discard 端口，明文连上去立刻 ECONNREFUSED —— 让健康探测秒回，不拖测试
# （换成 https 会因 TLS 重试拖到秒级）
DEAD_URL = "http://127.0.0.1:9/v1"

VALID_PROVIDER = {
    "name": "acme",
    "type": "openai_compat",
    "base_url": DEAD_URL,
    "models": ["acme-large", "acme-small"],
    "vendor": "acme",
    "capabilities": ["json_object", "tools"],
    "price": {"input": 0.001, "output": 0.002},
    "timeout_s": 10,
}


@pytest.fixture
def client(engine, session_factory, ai_config, monkeypatch):
    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.get_password", lambda svc, ref: "sk-test"
    )
    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.set_password", lambda svc, ref, key: None
    )
    # 健康探测会真打网络（本机对关闭端口要等约 4s 才失败）。这批用例考的是接入链路
    # 而不是网络行为，一律短路为 down，保持测试快且确定。
    monkeypatch.setattr(
        "app.ai.providers.openai_compat.OpenAICompatProvider._probe_health",
        lambda self: HEALTH_DOWN,
    )
    app = create_app()
    app.state.engine = engine
    app.state.session_factory = session_factory
    from app.agents.demo_stage import register_all
    from app.ai.budget import BudgetManager
    from app.ai.client import LlmGateway
    from app.ai.registry import ProviderRegistry
    from app.ai.routing import Router
    from app.orchestration.orchestrator import Orchestrator, StageRegistry

    ai_registry = ProviderRegistry.from_config(ai_config)
    router = Router.from_config(ai_config, ai_registry.providers_map())
    gateway = LlmGateway(ai_registry, router, BudgetManager(ai_config["ai"]["budget"]))
    app.state.ai_registry = ai_registry
    app.state.router = router
    app.state.gateway = gateway

    registry = StageRegistry()
    register_all(registry)
    app.state.orchestrator = Orchestrator(registry, gateway)
    with TestClient(app) as c:
        yield c


# ── 配置校验（纯函数层）───────────────────────────

def test_normalize_canonicalizes_legacy_and_list_fields():
    out = normalize("acme", {
        "type": "openai_compat", "base_url": DEAD_URL + "/",
        "model": "only-one-model",             # 旧的单数写法
        "vendor": "acme",
        "price_per_1k": {"input": "0.5", "output": 1},   # 旧的字段名 + 字符串数字
    })
    assert out["models"] == ["only-one-model"]
    assert out["base_url"] == DEAD_URL                      # 尾部斜杠被去掉
    assert out["price"] == {"input": 0.5, "output": 1.0}
    assert out["capabilities"] == ["json_object"]            # 未声明按保守默认
    assert out["api_key_ref"] == "acme"                      # 未指定则引用名即 provider 名


def test_normalize_keeps_blank_api_key_ref_as_keyless():
    """api_key_ref 显式留空 = 该端点无需鉴权（本地自建端点的常见形态）。"""
    out = normalize("local", {
        "type": "openai_compat", "base_url": "http://127.0.0.1:11434/v1",
        "models": ["qwen3"], "vendor": "local", "api_key_ref": "",
    })
    assert out["api_key_ref"] == ""

    provider = OpenAICompatProvider("local", out)
    assert provider.auth_required is False
    assert provider._api_key() is None          # 不再抛「未配置 API Key」
    assert provider._headers() == {}


@pytest.mark.parametrize("bad,reason", [
    ("", "必填"),
    ("ftp://api.acme.test/v1", "http"),
    ("https://", "主机名"),
    ("https://user:pw@api.acme.test/v1", "用户名"),
    ("https://api.acme.test/v1?k=1", "查询串"),
    ("http://169.254.169.254/latest/meta-data", "链路本地"),
    ("http://0.0.0.0:8000/v1", "未指定地址"),
])
def test_base_url_validation_rejects(bad, reason):
    with pytest.raises(ProviderConfigError, match=reason):
        normalize_base_url(bad)


@pytest.mark.parametrize("ok_url", [
    "https://127.0.0.1:11434/v1",     # 本机自建端点（设计 §4.2 明确支持）
    "https://api.openai.com/v1",
])
def test_base_url_validation_allows(ok_url):
    assert normalize_base_url(ok_url) == ok_url


@pytest.mark.parametrize("override,reason", [
    ({"name": "bad name"}, "id 非法"),
    ({"name": "-leading"}, "id 非法"),
    ({"type": "nope"}, "未知 provider 类型"),
    ({"vendor": ""}, "vendor 必填"),
    ({"models": []}, "models 不能为空"),
    ({"models": ["a", "a"]}, "重复"),
    ({"capabilities": ["telepathy"]}, "未知能力"),
    ({"price": {"input": -1}}, "不能为负"),
    ({"timeout_s": 0}, "必须大于 0"),
])
def test_normalize_rejects_bad_fields(override, reason):
    payload = {**VALID_PROVIDER, **override}
    provider_id = payload.pop("name")   # 这里的 name 是被当作身份（id）去校验
    with pytest.raises(ProviderConfigError, match=reason):
        normalize(provider_id, payload)


def test_normalize_label_is_decoupled_from_id():
    """展示名可中文、可重复，缺省回落到 id；身份始终是 id。"""
    payload = {k: v for k, v in VALID_PROVIDER.items() if k != "name"}
    assert normalize("acme", payload)["name"] == "acme"                    # 缺省回落
    assert normalize("acme", {**payload, "name": "我的智谱端点"})["name"] == "我的智谱端点"
    with pytest.raises(ProviderConfigError, match="名称过长"):
        normalize("acme", {**payload, "name": "x" * 65})
    with pytest.raises(ProviderConfigError, match="换行"):
        normalize("acme", {**payload, "name": "a\nb"})


# ── API：增删改 ──────────────────────────────────

def test_create_provider_takes_effect_without_restart(client):
    assert client.post("/api/settings/providers", json=VALID_PROVIDER).status_code == 200

    rows = {r["id"]: r for r in client.get("/api/settings/providers").json()}
    assert "acme" in rows
    row = rows["acme"]
    assert row["name"] == "acme"                             # 未给展示名则回落到 id
    assert row["models"] == ["acme-large", "acme-small"]
    assert row["capabilities"] == ["json_object", "tools"]
    assert row["price"] == {"input": 0.001, "output": 0.002}
    assert row["source"] == "user" and row["deletable"] is True
    assert row["health"] in ("ok", "down", "unconfigured")   # 已进入注册表并被探测


def test_create_provider_conflicts_only_on_explicit_id(client):
    """id 是身份：显式给的 id 撞了必须拒；没给 id 时按展示名派生并自动避让。

    展示名只是标签、可以重复 —— 那正是「名称可改」的前提；旧实现把它当身份，
    于是「改名」只能靠删掉重建，引用全断。
    """
    assert client.post("/api/settings/providers", json=VALID_PROVIDER).status_code == 200

    dup = client.post("/api/settings/providers", json={**VALID_PROVIDER, "id": "acme"})
    assert dup.status_code == 409
    assert "id 已存在" in dup.json()["detail"]

    same_label = client.post("/api/settings/providers",
                             json={**VALID_PROVIDER, "base_url": "http://127.0.0.1:9/v2"})
    assert same_label.status_code == 200
    assert same_label.json()["provider"] == "acme-2"   # 自动避让，不覆盖已有配置


def test_generate_id_handles_non_ascii_labels():
    """纯中文名派不出合法 id，得退化成可用的标识而不是报错。"""
    from app.ai.provider_config import generate_id

    assert generate_id("我的本地端点", set()) == "provider"
    assert generate_id("我的本地端点", {"provider"}) == "provider-2"
    assert generate_id("My Endpoint!!", set()) == "my-endpoint"


def test_rename_provider_keeps_identity_and_references(client):
    """改名只换展示名：id、路由引用、凭据引用全部不动。"""
    client.post("/api/settings/providers", json=VALID_PROVIDER)
    client.patch("/api/settings/routing", json={
        "tier": "write", "candidates": [{"provider": "acme", "model": "acme-small"}],
    })

    resp = client.patch("/api/settings/providers/acme", json={"name": "我的智谱端点"})
    assert resp.status_code == 200
    assert resp.json()["config"]["name"] == "我的智谱端点"

    row = next(r for r in client.get("/api/settings/providers").json() if r["id"] == "acme")
    assert row["name"] == "我的智谱端点"
    assert row["api_key_ref"] == "acme"                  # 凭据引用跟着 id，不跟展示名
    assert row["auth_required"] is True and row["has_key"] is True   # 编辑页据此回填
    assert row["referenced_by"] == ["routing:write"]     # 路由引用不断
    assert client.get("/api/settings/routing").json()["write"] == [
        {"provider": "acme", "model": "acme-small"}
    ]


def test_key_write_uses_credential_ref_not_provider_id(client, monkeypatch):
    """Key 写到 ``api_key_ref`` 名下 —— 多个端点可以共用一份凭据。"""
    calls: list[tuple] = []
    monkeypatch.setattr("app.api.settings.keyring.set_password",
                        lambda svc, ref, key: calls.append((ref, key)))

    client.post("/api/settings/providers",
                json={**VALID_PROVIDER, "api_key_ref": "shared-creds"})
    resp = client.put("/api/settings/providers/acme/key", json={"key": "sk-1"})
    assert resp.json() == {"provider": "acme", "api_key_ref": "shared-creds", "stored": True}
    assert calls == [("shared-creds", "sk-1")]


def test_key_write_refused_for_keyless_provider(client):
    """无需鉴权的端点不该被要求录 Key —— 直接说清原因，而不是写进一个没人读的账号。"""
    client.post("/api/settings/providers", json={**VALID_PROVIDER, "api_key_ref": ""})
    resp = client.put("/api/settings/providers/acme/key", json={"key": "sk-1"})
    assert resp.status_code == 400
    assert "无需鉴权" in resp.json()["detail"]


def test_create_provider_rejects_bad_base_url(client):
    bad = {**VALID_PROVIDER, "base_url": "http://169.254.169.254/v1"}
    resp = client.post("/api/settings/providers", json=bad)
    assert resp.status_code == 400
    assert "链路本地" in resp.json()["detail"]


def test_update_provider_patches_only_given_fields(client):
    client.post("/api/settings/providers", json=VALID_PROVIDER)

    resp = client.patch("/api/settings/providers/acme",
                        json={"models": ["acme-x"], "price": {"input": 0.01, "output": 0.02}})
    assert resp.status_code == 200
    config = resp.json()["config"]
    assert config["models"] == ["acme-x"]
    assert config["price"] == {"input": 0.01, "output": 0.02}
    assert config["vendor"] == "acme"                 # 未提交的字段沿用现值
    assert config["capabilities"] == ["json_object", "tools"]


def test_update_provider_rejects_when_new_capabilities_break_routing(client):
    """把档位绑到一个 provider 上，再收回它必备的能力 → 落盘前就该被拦下。"""
    client.post("/api/settings/providers", json=VALID_PROVIDER)
    bound = client.patch("/api/settings/routing", json={
        "tier": "plan", "candidates": [{"provider": "acme", "model": "acme-large"}],
    })
    assert bound.status_code == 200

    resp = client.patch("/api/settings/providers/acme", json={"capabilities": []})
    assert resp.status_code == 400
    assert "已保持原样" in resp.json()["detail"]

    row = next(r for r in client.get("/api/settings/providers").json() if r["id"] == "acme")
    assert row["capabilities"] == ["json_object", "tools"]   # 原值未被改动

    # 运行期也必须仍是可用的旧配置：plan 档还能正常取到候选
    assert client.get("/api/settings/routing").json()["plan"] == [
        {"provider": "acme", "model": "acme-large"}
    ]


def test_builtin_provider_cannot_be_deleted(client):
    resp = client.delete("/api/settings/providers/mock")
    assert resp.status_code == 400
    assert "内置 provider" in resp.json()["detail"]


def test_delete_referenced_provider_is_refused_with_locations(client):
    client.post("/api/settings/providers", json=VALID_PROVIDER)
    client.patch("/api/settings/routing", json={
        "tier": "write", "candidates": [{"provider": "acme", "model": "acme-small"}],
    })

    resp = client.delete("/api/settings/providers/acme")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["referenced_by"] == ["routing:write"]

    # 解除引用后即可删除
    client.patch("/api/settings/routing", json={
        "tier": "write", "candidates": [{"provider": "mock", "model": "mock-small"}],
    })
    assert client.delete("/api/settings/providers/acme").status_code == 200
    assert "acme" not in [r["id"] for r in client.get("/api/settings/providers").json()]


def test_delete_unknown_provider_is_404(client):
    assert client.delete("/api/settings/providers/nobody").status_code == 404


# ── API：探测模型与类型清单 ───────────────────────

def test_probe_models_returns_ids_only(client, monkeypatch):
    """上游清单里只有 id 可用；能力与价格一律以本地配置为准。"""
    captured: dict = {}

    def fake_list(self, base_url=None, api_key=None):
        captured["base_url"] = self.base_url
        return ["acme-large", "acme-small"]

    monkeypatch.setattr(OpenAICompatProvider, "list_remote_models", fake_list)
    resp = client.post("/api/settings/providers/acme/probe-models",
                       json={"base_url": DEAD_URL})
    assert resp.status_code == 200
    assert resp.json() == {
        "provider": "acme", "ok": True,
        "models": ["acme-large", "acme-small"], "detail": None,
    }
    assert captured["base_url"] == DEAD_URL


def test_probe_models_failure_asks_for_manual_input(client, monkeypatch):
    def boom(self, base_url=None, api_key=None):
        raise RuntimeError("endpoint has no /models")

    monkeypatch.setattr(OpenAICompatProvider, "list_remote_models", boom)
    resp = client.post("/api/settings/providers/acme/probe-models",
                       json={"base_url": DEAD_URL})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False and body["models"] == []
    assert "手工填写模型 ID" in body["detail"]


def test_probe_models_unknown_name_without_base_url_is_404(client):
    """既不在配置里、又没带 base_url 的探测请求没有可探测的对象。"""
    resp = client.post("/api/settings/providers/brandnew/probe-models", json={})
    assert resp.status_code == 404
    assert "base_url" in resp.json()["detail"]


def test_probe_models_honours_keyless_draft(client, monkeypatch):
    """勾了「无需鉴权」的草稿探测时不该要 Key（用户投诉的场景）。

    旧实现写 `raw.get("api_key_ref") or name`，把**显式留空**当成「没填」，
    于是「无需鉴权」在探测路径上完全失效：本地自建端点永远探测不了。
    """
    seen: dict = {}

    def fake_list(self, base_url=None, api_key=None):
        seen["auth_required"] = self.auth_required
        seen["api_key_ref"] = self.api_key_ref
        return ["local-model"]

    monkeypatch.setattr(OpenAICompatProvider, "list_remote_models", fake_list)

    body = client.post("/api/settings/providers/brandnew/probe-models",
                       json={"base_url": DEAD_URL, "keyless": True}).json()
    assert body["ok"] is True and body["models"] == ["local-model"]
    assert seen == {"auth_required": False, "api_key_ref": ""}

    # 不勾选则仍按缺省语义要求凭据（引用名即 provider 名）
    client.post("/api/settings/providers/brandnew/probe-models",
                json={"base_url": DEAD_URL})
    assert seen == {"auth_required": True, "api_key_ref": "brandnew"}


def test_probe_models_accepts_shared_credential_ref(client, monkeypatch):
    """多个端点共用一份凭据：草稿能指定 api_key_ref，不必等于 provider 名。"""
    seen: dict = {}

    def fake_list(self, base_url=None, api_key=None):
        seen["api_key_ref"] = self.api_key_ref
        return ["m"]

    monkeypatch.setattr(OpenAICompatProvider, "list_remote_models", fake_list)
    client.post("/api/settings/providers/brandnew/probe-models",
                json={"base_url": DEAD_URL, "api_key_ref": "shared-creds"})
    assert seen["api_key_ref"] == "shared-creds"


def test_provider_types_endpoint(client):
    body = client.get("/api/settings/provider-types").json()
    assert body["types"] == ["mock", "openai_compat"]
    assert set(body["capabilities"]) == {"json_object", "tools", "stream", "vision"}


# ── 自定义头 / 免鉴权端点 ─────────────────────────

def test_extra_headers_are_sent_and_override_bearer():
    provider = OpenAICompatProvider("local", {
        "base_url": DEAD_URL, "models": ["m"], "vendor": "local",
        "api_key_ref": "",                        # 免鉴权
        "extra_headers": {"x-api-key": "abc"},
    })
    assert provider._headers() == {"x-api-key": "abc"}


def test_extra_body_is_merged_into_payload(monkeypatch):
    """extra_body 必须真的进请求体，否则这个配置项等于谎报能力。"""
    provider = OpenAICompatProvider("acme", {
        "base_url": DEAD_URL, "models": ["m"], "vendor": "acme",
        "api_key_ref": "",                       # 免鉴权，避免真去读凭据管理器
        "extra_body": {"top_p": 0.9},
    })
    seen: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    monkeypatch.setattr("httpx.Client.post",
                        lambda self, url, json=None, headers=None: (
                            seen.update(json or {}, url=url) or FakeResponse()))

    provider.complete(ChatRequest(messages=[ChatMessage(role="user", content="x")]))
    assert seen["top_p"] == 0.9
    assert seen["model"] == "m"
