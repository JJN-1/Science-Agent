from __future__ import annotations

import pytest

from app.ai.base import CircuitOpen, ProviderUnavailable
from app.ai.registry import ProviderRegistry
from app.ai.routing import RouteCandidate, RoutingError, Router
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

def test_unhealthy_provider_excluded():
    reg = ProviderRegistry.from_config(_config(healthy=False))
    assert "a" not in reg.names()
    assert reg.names() == ["b"]


def test_unknown_provider_type_rejected():
    with pytest.raises(ValueError, match="未知 provider 类型"):
        ProviderRegistry.from_config(_config(type="magic"))


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


def test_reload_replaces_providers():
    reg = ProviderRegistry.from_config(_config())
    delta = reg.reload(_config(healthy=False))  # a 不健康被剔除，b 保留
    assert reg.names() == ["b"]
    assert delta["removed"] == ["a"]


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
    for name, cfg in {"a": base, **overrides}.items():
        out[name] = MockProvider(name, cfg)
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
