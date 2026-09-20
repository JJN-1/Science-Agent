from __future__ import annotations

import time

import pytest

from app.ai.base import (
    HEALTH_DOWN,
    HEALTH_OK,
    HEALTH_UNCONFIGURED,
    ChatMessage,
    ChatRequest,
    CircuitOpen,
    ProviderUnavailable,
)
from app.ai.providers.mock import MockProvider
from app.ai.registry import PROVIDER_TYPES, ProviderRegistry
from app.ai.routing import RouteCandidate, Router, RoutingError
from app.store.dao import agents as agents_dao
from app.store.dao import approvals as approvals_dao
from app.store.dao import decisions as decisions_dao
from app.store.dao import usage as usage_dao


def _config(**provider_overrides) -> dict:
    a = {"type": "mock", "model": "m1", "vendor": "mock",
         "capabilities": ["json_object"]}
    a.update(provider_overrides)
    b = {"type": "mock", "model": "m2", "vendor": "mock",
         "capabilities": ["json_object"]}
    return {
        "ai": {
            "providers": {"a": a, "b": b},
            "circuit": {"failure_threshold": 3, "cooldown_seconds": 0.05},
            "routing": {
                "extract": [{"provider": "a", "model": "m1"}],
                "plan": [{"provider": "a", "model": "m1"}],
                "critique": [{"provider": "b", "model": "m2"}],
                "synthesize": [{"provider": "a", "model": "m1"}],
                "write": [{"provider": "a", "model": "m1"}],
            },
        }
    }


# ── ProviderRegistry ─────────────────────────────

def test_unhealthy_provider_kept_but_flagged():
    """FIX-04：不健康的 provider 不再被静默剔除，而是保留并以健康状态标注。

    旧行为是直接 `continue` 丢掉，于是 Router 紧接着报「引用了不存在的 provider」，
    应用启动失败且错误指向错误方向。
    """
    reg = ProviderRegistry.from_config(_config(healthy=False))
    assert reg.names() == ["a", "b"]  # 仍然在注册表里
    report = {row["name"]: row for row in reg.health_report()}
    assert report["a"]["health"] == HEALTH_DOWN
    assert report["a"]["healthy"] is False
    assert report["a"]["detail"]  # 带可操作提示
    assert report["b"]["health"] == HEALTH_OK
    with pytest.raises(ProviderUnavailable):
        reg.check_available("a")  # 调用点才判定不可用


def test_unknown_provider_type_rejected():
    with pytest.raises(ValueError, match="未知 provider 类型"):
        ProviderRegistry.from_config(_config(type="magic"))


def test_openai_compat_provider_registered():
    """FIX-01：真实模型 provider 必须能注册，否则配了真实模型应用直接起不来。"""
    assert "openai_compat" in PROVIDER_TYPES
    cfg = {
        "ai": {
            "providers": {
                "deepseek": {
                    "type": "openai_compat", "model": "deepseek-chat",
                    "vendor": "deepseek", "base_url": "https://api.deepseek.com/v1",
                    "api_key_ref": "deepseek",
                }
            },
            "routing": {},
        }
    }
    reg = ProviderRegistry.from_config(cfg)  # 不得抛异常
    assert reg.names() == ["deepseek"]
    row = reg.health_report()[0]
    assert row["health"] == HEALTH_UNCONFIGURED  # 未录 Key 属于「待配置」而非「故障」
    assert "API Key" in row["detail"]


def test_openai_compat_config_to_router_integration(monkeypatch):
    """FIX-01/04 集成：配置文件 → ProviderRegistry → Router 全链路。

    这正是此前完全缺失、导致 P0-1 不可见的测试类型（所有测试都只用 mock）。
    """
    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.get_password",
        lambda svc, ref: None,  # 模拟「配置写好了但 Key 还没录」
    )
    cfg = {
        "ai": {
            "providers": {
                "mock": {"type": "mock", "model": "m", "vendor": "mock",
                         "capabilities": ["json_object"]},
                "acme": {"type": "openai_compat", "model": "acme-1", "vendor": "acme",
                         "base_url": "https://api.acme.test/v1", "api_key_ref": "acme"},
            },
            "routing": {
                "extract": [{"provider": "mock", "model": "m"}],
                "plan": [{"provider": "acme", "model": "acme-1"}],
                "critique": [{"provider": "mock", "model": "m"}],
                "synthesize": [{"provider": "mock", "model": "m"}],
                "write": [{"provider": "mock", "model": "m"}],
            },
        }
    }
    reg = ProviderRegistry.from_config(cfg)
    router = Router.from_config(cfg, reg.providers_map())  # 不得抛「不存在的 provider」
    assert router.candidates("plan")[0].provider == "acme"


def test_circuit_breaker_trips_and_recovers():
    reg = ProviderRegistry.from_config(_config())
    for _ in range(3):
        reg.record_failure("a")
    with pytest.raises(CircuitOpen):
        reg.check_available("a")
    import time

    time.sleep(0.06)  # 超过冷却期 → 半开
    reg.check_available("a")
    reg.record_success("a")
    reg.check_available("a")


def test_reload_picks_up_config_changes():
    """FIX-04 之后 health 不再决定 provider 的进出，热重载语义回归「跟随配置变化」。"""
    reg = ProviderRegistry.from_config(_config())
    assert reg.names() == ["a", "b"]

    shrunk = _config()
    shrunk["ai"]["providers"].pop("a")
    delta = reg.reload(shrunk)
    assert reg.names() == ["b"]
    assert delta == {"removed": ["a"], "added": []}

    delta = reg.reload(_config())
    assert reg.names() == ["a", "b"]
    assert delta == {"removed": [], "added": ["a"]}


def test_snapshot_freezes_models():
    reg = ProviderRegistry.from_config(_config())
    snap = reg.snapshot()
    assert snap == {"a": "m1", "b": "m2"}


# ── Router ───────────────────────────────────────

def _providers(**overrides) -> dict:
    from app.ai.providers.mock import MockProvider

    base = {"type": "mock", "model": "m", "vendor": "mock",
            "capabilities": ["json_object"]}
    out = {}
    # 逐字段覆盖而非整体替换：用例只想改某一项，其余保持基线
    for name, extra in {"a": {}, **overrides}.items():
        out[name] = MockProvider(name, {**base, **extra})
    return out


def _routes(critique_provider="a") -> dict:
    return {
        "extract": [{"provider": "a", "model": "m"}],
        "plan": [{"provider": "a", "model": "m"}],
        "critique": [{"provider": critique_provider, "model": "m"}],
        "synthesize": [{"provider": "a", "model": "m"}],
        "write": [{"provider": "a", "model": "m"}],
    }


def test_router_valid_and_candidates():
    router = Router.from_config({"ai": {"routing": _routes()}}, _providers())
    assert router.candidates("plan")[0].provider == "a"


def test_router_rejects_unknown_provider():
    with pytest.raises(RoutingError, match="不存在的 provider"):
        Router.from_config({"ai": {"routing": _routes()}}, {})


def test_router_rejects_missing_capability():
    with pytest.raises(RoutingError, match="缺少能力"):
        Router.from_config({"ai": {"routing": _routes()}},
                           _providers(a={"type": "mock", "vendor": "mock",
                                         "capabilities": []}))


def test_router_rejects_missing_tier():
    cfg = {"ai": {"routing": {k: v for k, v in _routes().items() if k != "write"}}}
    with pytest.raises(RoutingError, match="未配置路由"):
        Router.from_config(cfg, _providers())


def test_router_rejects_same_vendor_critique():
    with pytest.raises(RoutingError, match="交叉验证"):
        Router.from_config({"ai": {"routing": _routes()}},
                           _providers(a={"type": "mock", "vendor": "acme",
                                         "capabilities": ["json_object"]}))


def test_router_update_tier_hot_swap():
    router = Router.from_config({"ai": {"routing": _routes()}}, _providers())
    router.update_tier("plan", [RouteCandidate(provider="a", model="m2")],
                       _providers())
    assert router.candidates("plan")[0].model == "m2"


# ── DAO：usage / decisions / approvals / agents ──

def test_usage_record_and_summary(session):
    from app.store.dao import projects as projects_dao

    p = projects_dao.create(session, title="t", domain="cs-ai")
    usage_dao.record(session, stage_id="S1", agent_id="scout", provider="mock",
                     model="m", tier="plan", prompt_tokens=100,
                     completion_tokens=50, cost=0.5)
    usage_dao.record(session, stage_id="S2", agent_id="librarian", provider="mock",
                     model="m", tier="synthesize", prompt_tokens=10,
                     completion_tokens=5, cost=0.25)
    session.flush()
    assert usage_dao.project_spend(session, project_id=p.id) == 0.0  # 未关联项目不计
    usage_dao.record(session, stage_id="S1", agent_id="scout", provider="mock",
                     model="m", tier="plan", cost=1.0, project_id=p.id)
    session.flush()
    assert usage_dao.project_spend(session, project_id=p.id) == 1.0
    by_agent = {r["key"]: r["cost"] for r in
                usage_dao.summary_by(session, project_id=p.id, dim="agent")}
    assert by_agent == {"scout": 1.0}


def test_failed_call_counts_but_costs_nothing(session):
    """失败记账：``calls`` 含失败、``cost`` 不含。

    用户投诉「供应商统计里没有那条请求」—— 只记成功时，跑挂的那次在数据上不存在，
    「到底发出去没有、发给了谁」就成了无解的问题。钱没花出去和事情没发生是两回事。
    """
    from app.store.dao import projects as projects_dao

    p = projects_dao.create(session, title="t", domain="cs-ai")
    usage_dao.record(session, stage_id="S1", agent_id="scout", provider="mock",
                     model="m", tier="plan", cost=0.5, project_id=p.id)
    usage_dao.record(session, stage_id="S1", agent_id="scout", provider="mock",
                     model="m", tier="plan", project_id=p.id,
                     status=usage_dao.STATUS_FAILED,
                     error="LLM-UNAVAIL-001: mock unavailable", attempts=2)
    session.flush()

    assert usage_dao.project_spend(session, project_id=p.id) == 0.5  # 失败不花钱
    assert usage_dao.summary_by(session, project_id=p.id, dim="provider") == [
        {"key": "mock", "calls": 2, "failed": 1,
         "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.5}
    ]


def test_decisions_add_and_list(session):
    from app.store.dao import projects as projects_dao

    p = projects_dao.create(session, title="t", domain="cs-ai")
    decisions_dao.add(session, project_id=p.id, stage_id="S1", agent_id="scout",
                      decision="选用 GNN", reason="图谱结构匹配")
    decisions_dao.add(session, project_id=p.id, stage_id="S2", agent_id="librarian",
                      decision="检索失败", kind="failed_attempt")
    session.flush()
    rows = decisions_dao.list_for_project(session, p.id)
    assert [r.kind for r in rows] == ["decision", "failed_attempt"]


def test_approvals_lifecycle(session):
    from app.store.dao import projects as projects_dao

    p = projects_dao.create(session, title="t", domain="cs-ai")
    row = approvals_dao.create(session, project_id=p.id, kind="budget",
                               detail={"reason": "超出单项目总额"})
    session.flush()
    assert len(approvals_dao.list_by_status(session, status="pending")) == 1
    approvals_dao.decide(session, row.id, "approved")
    session.flush()
    assert approvals_dao.list_by_status(session, status="pending") == []
    assert approvals_dao.get(session, row.id).status == "approved"


def test_agents_upsert_idempotent(session):
    agents_dao.upsert(session, "scout", "Scout", tier="plan")
    agents_dao.upsert(session, "scout", "Scout", tier="extract")
    session.flush()
    rows = agents_dao.list_all(session)
    assert len(rows) == 1
    assert rows[0].tier == "extract"


def test_mock_delay_ms_actually_sleeps():
    """delay_ms 是真延迟，不是元数据。

    US-311 / US-304 的前端验收都说「长任务全程有进度」——没有真实延迟，
    任务一瞬间就跑完了，流式进度根本无从观察。
    """
    provider = MockProvider("mock", {"models": ["m"], "delay_ms": 80})
    request = ChatRequest(messages=[ChatMessage(role="user", content="hi")])

    started = time.monotonic()
    provider.complete(request)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.06, f"delay_ms 未生效（耗时 {elapsed * 1000:.0f}ms）"
    assert MockProvider("mock", {"models": ["m"]}).complete(request).latency_ms == 0


