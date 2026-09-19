from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(engine, session_factory, ai_config, monkeypatch):
    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.get_password", lambda svc, ref: "sk-test"
    )
    monkeypatch.setattr(
        "app.ai.providers.openai_compat.keyring.set_password", lambda svc, ref, key: None
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


def _make_project(client) -> int:
    return client.post("/api/projects", json={"title": "治理项目", "goal": "G"}).json()["id"]


# ── US-204：决策日志 ─────────────────────────────

def test_decisions_recorded_and_listed(client, run_and_wait):
    project_id = _make_project(client)
    run_and_wait(client, project_id, "S1")
    rows = client.get(f"/api/projects/{project_id}/decisions").json()
    assert len(rows) == 1
    assert rows[0]["kind"] == "decision"
    assert rows[0]["agent_id"] == "scout"
    assert "候选研究问题" in rows[0]["decision"]


# ── US-203：成本归因 ─────────────────────────────

def test_usage_summary_by_agent(client, run_and_wait):
    project_id = _make_project(client)
    run_and_wait(client, project_id, "S1")
    data = client.get(f"/api/usage/summary?project_id={project_id}&dim=agent").json()
    assert data["dim"] == "agent"
    assert data["rows"][0]["key"] == "scout"
    assert data["rows"][0]["calls"] == 1
    assert data["total_cost"] >= 0
    assert client.get(f"/api/usage/summary?project_id={project_id}&dim=bogus").status_code == 400


# ── US-205：审批暂停与恢复 ────────────────────────

def test_budget_pause_and_approve_resume(client, session_factory, run_and_wait):
    from app.store.dao import agents as agents_dao

    project_id = _make_project(client)
    with session_factory() as s:
        agent = agents_dao.get_by_agent_id(s, "scout")  # lifespan 已播种
        agent.budget_steps = 0
        s.commit()

    _, job = run_and_wait(client, project_id, "S1")
    assert job["status"] == "paused"

    pending = client.get(f"/api/approvals?project_id={project_id}").json()
    assert len(pending) == 1 and pending[0]["status"] == "pending"

    # 恢复 Agent 步数预算后批准 → 重跑成功
    with session_factory() as s:
        agent = agents_dao.get_by_agent_id(s, "scout")
        agent.budget_steps = 20
        s.commit()
    approved = client.post(f"/api/approvals/{pending[0]['id']}/approve", json={}).json()
    assert approved["status"] == "approved"
    assert approved["new_run_id"] is not None
    detail = client.get(f"/api/runs/{approved['new_run_id']}").json()
    assert detail["status"] == "succeeded"


def test_reject_approval(client, session_factory, run_and_wait):
    from app.store.dao import agents as agents_dao

    project_id = _make_project(client)
    with session_factory() as s:
        agent = agents_dao.get_by_agent_id(s, "scout")
        agent.budget_steps = 0
        s.commit()
    run_and_wait(client, project_id, "S1")
    pending = client.get(f"/api/approvals?project_id={project_id}").json()
    rejected = client.post(f"/api/approvals/{pending[0]['id']}/reject", json={}).json()
    assert rejected["status"] == "rejected"
    assert "new_run_id" not in rejected
    assert client.get(f"/api/approvals?project_id={project_id}").json() == []


# ── US-206：设置 / 健康 / 热重载 / Key ────────────

def test_providers_health(client):
    rows = client.get("/api/settings/providers").json()
    assert rows[0]["name"] == "mock"
    assert rows[0]["healthy"] is True
    assert rows[0]["health"] == "ok"  # FIX-04：新增三态字段
    assert rows[0]["detail"] is None
    assert "json_object" in rows[0]["capabilities"]


def test_routing_patch_valid_and_invalid(client):
    ok = client.patch("/api/settings/routing", json={
        "tier": "plan", "candidates": [{"provider": "mock", "model": "mock-large"}],
    })
    assert ok.status_code == 200
    assert ok.json()["candidates"][0]["model"] == "mock-large"
    assert client.get("/api/settings/routing").json()["plan"][0]["model"] == "mock-large"

    bad = client.patch("/api/settings/routing", json={
        "tier": "plan", "candidates": [{"provider": "ghost", "model": "x"}],
    })
    assert bad.status_code == 400


def test_provider_key_write(client):
    resp = client.put("/api/settings/providers/deepseek/key", json={"key": "sk-123"})
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
