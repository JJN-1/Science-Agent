"""FIX-02 预算豁免：DAO 层与 API 层的完整回归。

单独成文件是因为这组测试跨越 FIX-02 的两端——「有效限额计算」与「批准即生效」，
且需要复现修复前那个「批准 → 立刻再次熔断」的死循环。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store.dao import agents as agents_dao
from app.store.dao import grants as grants_dao
from app.store.dao import projects as projects_dao
from app.store.dao import runs as runs_dao
from app.store.dao import usage as usage_dao

# ── DAO / BudgetManager 层 ───────────────────────

def test_grants_scoped_and_expirable(session):
    project = projects_dao.create(session, title="豁免项目", domain="cs-ai")
    session.flush()

    grants_dao.create(session, project_id=project.id, scope="agent_steps",
                      amount=20, agent_id="scout")
    session.flush()
    assert grants_dao.granted(session, project.id, "agent_steps", agent_id="scout") == 20
    # 作用域隔离：别的 agent、别的 scope 都不受影响
    assert grants_dao.granted(session, project.id, "agent_steps", agent_id="other") == 0
    assert grants_dao.granted(session, project.id, "agent_cost", agent_id="scout") == 0

    now = datetime.now(UTC)
    grants_dao.create(session, project_id=project.id, scope="project_total",
                      amount=99, expires_at=now - timedelta(hours=1))
    session.flush()
    assert grants_dao.granted(session, project.id, "project_total") == 0  # 过期不计

    grants_dao.create(session, project_id=project.id, scope="project_total",
                      amount=5, expires_at=now + timedelta(hours=1))
    session.flush()
    assert grants_dao.granted(session, project.id, "project_total") == 5


def test_budget_effective_limit_includes_grants(session):
    """FIX-02 核心：有效限额 = 配置限额 + 未过期豁免。"""
    from app.ai.budget import BudgetExceeded, BudgetManager

    project = projects_dao.create(session, title="预算项目", domain="cs-ai")
    agents_dao.upsert(session, agent_id="scout", name="Scout", tier="plan",
                      budget_steps=0, budget_cost=100.0)
    session.flush()
    run = runs_dao.create_run(session, project_id=project.id, stage_id="S1",
                              agent_id="scout")
    runs_dao.add_step(session, run_id=run.id, kind="thought", content={"text": "x"})

    manager = BudgetManager({"project_total": 50.0, "project_daily": 10.0})
    assert manager.suggested_grant("agent_steps") == 20.0  # 默认策略

    with pytest.raises(BudgetExceeded) as exc:
        manager.check(session, project.id, "scout", run.id)
    assert exc.value.kind == "agent_steps"
    assert exc.value.detail["limit"] == 0

    grants_dao.create(
        session, project_id=project.id, scope="agent_steps", amount=20,
        agent_id="scout", expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    session.flush()
    manager.check(session, project.id, "scout", run.id)  # 豁免生效 → 不再熔断


def test_project_total_grant_raises_limit(session):
    from app.ai.budget import BudgetExceeded, BudgetManager

    project = projects_dao.create(session, title="总额项目", domain="cs-ai")
    session.flush()
    usage_dao.record(session, stage_id="S1", agent_id="scout", provider="mock",
                     model="m", tier="plan", cost=60.0, project_id=project.id)
    session.flush()

    manager = BudgetManager({"project_total": 50.0, "project_daily": 1000.0})
    with pytest.raises(BudgetExceeded) as exc:
        manager.check(session, project.id, "ghost", 1)  # 无此 agent → 只做项目级判定
    assert exc.value.kind == "project_total"

    grants_dao.create(session, project_id=project.id, scope="project_total", amount=20)
    session.flush()
    manager.check(session, project.id, "ghost", 1)  # 50 + 20 = 70 > 60 → 通过


# ── API 层：批准必须真的生效 ─────────────────────

@pytest.fixture
def client(engine, session_factory, ai_config):
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


def _pause_scout(client, session_factory) -> tuple[int, list]:
    """把 scout 步数预算压到 0 触发一次熔断，返回 (project_id, pending_approvals)。"""
    project_id = client.post(
        "/api/projects", json={"title": "预算项目", "goal": "G"}
    ).json()["id"]
    with session_factory() as s:
        agent = agents_dao.get_by_agent_id(s, "scout")  # lifespan 已播种
        agent.budget_steps = 0
        s.commit()
    assert client.post(f"/api/projects/{project_id}/stages/S1/run").json()["status"] == "paused"
    return project_id, client.get(f"/api/approvals?project_id={project_id}").json()


def test_approve_issues_grant_and_resumes_without_manual_budget_edit(client, session_factory):
    """FIX-02 回归：批准本身必须恢复运行。

    修复前：批准只改 approvals.status，预算计数分文未动，重跑立刻再次熔断 ——
    表现为「批准 → 又弹一条新审批」的无限循环，演示时靠手工改库才走通。
    """
    project_id, pending = _pause_scout(client, session_factory)
    assert len(pending) == 1
    assert pending[0]["detail"]["suggested_grant"] == 20
    assert pending[0]["detail"]["agent_id"] == "scout"

    # 关键：这里**不再**手工改回 budget_steps
    approved = client.post(f"/api/approvals/{pending[0]['id']}/approve", json={}).json()
    assert approved["grant"]["scope"] == "agent_steps"
    assert approved["grant"]["amount"] == 20
    assert approved["grant"]["expires_at"] is not None
    assert client.get(f"/api/runs/{approved['new_run_id']}").json()["status"] == "succeeded"

    # 不再堆新的 pending 审批
    assert client.get(f"/api/approvals?project_id={project_id}").json() == []
    # 豁免有效期内持续生效
    assert client.post(f"/api/projects/{project_id}/stages/S1/run").json()["status"] == "succeeded"

    grants = client.get(f"/api/projects/{project_id}/budget-grants").json()
    assert len(grants) == 1
    assert grants[0]["approval_id"] == pending[0]["id"]  # 可回溯到审批单
    assert grants[0]["amount"] == 20


def test_grant_amount_is_a_real_constraint(client, session_factory):
    """豁免额度是真约束而非「放行开关」：批 0 额度，重跑仍然熔断。"""
    project_id, pending = _pause_scout(client, session_factory)
    approved = client.post(
        f"/api/approvals/{pending[0]['id']}/approve", json={"grant_amount": 0}
    ).json()
    assert approved["grant"]["amount"] == 0
    assert client.get(f"/api/runs/{approved['new_run_id']}").json()["status"] == "paused"


def test_approve_accepts_explicit_grant_amount(client, session_factory):
    project_id, pending = _pause_scout(client, session_factory)
    approved = client.post(
        f"/api/approvals/{pending[0]['id']}/approve", json={"grant_amount": 3.5}
    ).json()
    assert approved["grant"]["amount"] == 3.5
    assert client.get(f"/api/runs/{approved['new_run_id']}").json()["status"] == "succeeded"


def test_approval_records_decision_audit(client, session_factory):
    """谁批的、批了多少、有效期多久，都要能从决策日志里查出来。"""
    project_id, pending = _pause_scout(client, session_factory)
    client.post(f"/api/approvals/{pending[0]['id']}/approve", json={"note": "同意加预算"})
    rows = client.get(f"/api/projects/{project_id}/decisions").json()
    audits = [r for r in rows if r["decided_by"] == "user"]
    assert len(audits) == 1
    assert "追加 agent_steps 额度" in audits[0]["decision"]
    assert audits[0]["reason"] == "同意加预算"
    # decided_at 不再为 NULL
    decided = client.get(f"/api/approvals?project_id={project_id}&status=approved").json()
    assert decided[0]["decided_at"] is not None


def test_approval_survives_a_failed_resume(client, session_factory):
    """重跑失败不能把人的批准动作一起回滚——审批单「回到待审」是最让人困惑的状态。"""
    project_id, pending = _pause_scout(client, session_factory)
    client.app.state.ai_registry.get("mock")._fail_times = 999  # 让重跑时的模型调用失败

    approved = client.post(f"/api/approvals/{pending[0]['id']}/approve", json={}).json()
    assert approved["status"] == "approved"
    assert approved["grant"]["amount"] == 20
    assert approved["new_run_id"] is None
    assert approved["resume_error"]  # 失败原因要回给调用方

    assert client.get(f"/api/approvals?project_id={project_id}").json() == []  # 不再 pending
    assert len(client.get(f"/api/projects/{project_id}/budget-grants").json()) == 1


def test_reject_does_not_issue_grant(client, session_factory):
    project_id, pending = _pause_scout(client, session_factory)
    rejected = client.post(f"/api/approvals/{pending[0]['id']}/reject", json={}).json()
    assert "grant" not in rejected
    assert client.get(f"/api/projects/{project_id}/budget-grants").json() == []
