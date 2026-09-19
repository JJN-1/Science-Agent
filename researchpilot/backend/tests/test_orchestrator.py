from __future__ import annotations

import pytest

from app.agents.demo_stage import register_all
from app.orchestration.base import StageAgent
from app.orchestration.orchestrator import STAGE_ORDER, Orchestrator, StageRegistry
from app.store.dao import blackboard as bb_dao
from app.store.dao import checkpoints as cp_dao
from app.store.dao import decisions as decisions_dao
from app.store.dao import projects as projects_dao
from app.store.dao import runs as runs_dao
from sqlalchemy import text


@pytest.fixture
def orchestrator(gateway):
    registry = StageRegistry()
    register_all(registry)
    return Orchestrator(registry, gateway)


def test_registry_rejects_duplicate_stage():
    registry = StageRegistry()
    register_all(registry)
    duplicate = type(registry.get("S1"))()
    duplicate.stage_id = "S1"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(duplicate)


def test_run_stage_records_trajectory_checkpoint_blackboard_and_usage(
    session, orchestrator
):
    project = projects_dao.create(session, title="演示项目", goal="图神经网络推荐")
    run_id = orchestrator.run_stage(session, project.id, "S1")

    run = runs_dao.get_run(session, run_id)
    assert run.status == "succeeded"
    assert run.agent_id == "scout"
    steps = runs_dao.list_steps(session, run_id)
    assert [s.kind for s in steps] == ["thought", "llm_call", "decision"]
    llm_step = steps[1].content
    assert llm_step["provider"] == "mock" and llm_step["cost"] >= 0

    # US-203：llm_usage 记账已归因到项目/阶段/Agent
    usage_rows = session.execute(
        text("SELECT stage_id, agent_id, project_id, run_id FROM llm_usage")
    ).all()
    assert usage_rows == [("S1", "scout", project.id, run_id)]

    # US-204：决策日志已落库
    rows = decisions_dao.list_for_project(session, project.id)
    assert [r.kind for r in rows] == ["decision"]

    checkpoints = cp_dao.list_for_project(session, project.id)
    assert len(checkpoints) == 1 and checkpoints[0].stage_id == "S1"

    outputs = [
        o for o in bb_dao.list_for_project(session, project.id)
        if o.obj_type == "research_questions"
    ]
    assert len(outputs) == 1
    assert outputs[0].produced_by == "scout"
    assert len(outputs[0].payload["questions"]) == 2


def test_run_stage_failure_marks_run_failed_and_records_attempt(session, gateway):
    class FailingStage(StageAgent):
        stage_id, agent_id, name = "SX", "tester", "失败阶段"

        def run(self, ctx):
            ctx.think("即将失败")
            raise RuntimeError("boom")

    registry = StageRegistry()
    registry.register(FailingStage())
    orchestrator = Orchestrator(registry, gateway)
    project = projects_dao.create(session, title="p")

    with pytest.raises(RuntimeError, match="boom"):
        orchestrator.run_stage(session, project.id, "SX")

    run = runs_dao.list_for_project(session, project.id)[0]
    assert run.status == "failed" and "boom" in run.error
    checkpoints = cp_dao.list_for_project(session, project.id)
    assert checkpoints[0].status == "failed"
    # US-204：失败尝试自动记录
    rows = decisions_dao.list_for_project(session, project.id)
    assert [r.kind for r in rows] == ["failed_attempt"]


def test_run_pipeline_executes_in_order(session, orchestrator):
    project = projects_dao.create(session, title="全链路")
    run_ids = orchestrator.run_pipeline(session, project.id)
    assert len(run_ids) == 8
    stages_run = [r.stage_id for r in runs_dao.list_for_project(session, project.id)]
    assert stages_run == list(reversed(STAGE_ORDER))  # list_for_project 按新→旧排序
    assert len(cp_dao.list_for_project(session, project.id)) == 8
    # S1 产出 research_questions，S2–S8 产出 stage_output，各自版本递增
    versions = sorted(o.version for o in bb_dao.list_for_project(session, project.id))
    assert versions == [1, 1, 2, 3, 4, 5, 6, 7]


def test_run_pipeline_stops_on_paused_and_skips_rest(session, gateway):
    """FIX-07：一次预算熔断只产生 1 条审批，后续阶段标记 skipped。

    修复前 pipeline 会带着未决审批继续往下跑，堆出一串暂停与审批，
    治理语义（一次熔断 = 一次决策）完全失效。
    """
    from app.store.dao import agents as agents_dao
    from app.store.dao import approvals as approvals_dao

    registry = StageRegistry()
    register_all(registry)
    orchestrator = Orchestrator(registry, gateway)
    project = projects_dao.create(session, title="pipeline 暂停")
    agents_dao.upsert(session, agent_id="scout", name="Scout", tier="plan",
                      budget_steps=0, budget_cost=100.0)
    session.flush()

    run_ids = orchestrator.run_pipeline(session, project.id)
    assert len(run_ids) == 1  # S1 暂停即中断
    assert runs_dao.get_run(session, run_ids[0]).status == "paused"
    assert len(approvals_dao.list_by_status(session, project_id=project.id)) == 1

    statuses = {c.stage_id: c.status for c in cp_dao.list_for_project(session, project.id)}
    assert statuses["S1"] == "paused"
    for stage_id in ("S2", "S3", "S4", "S5", "S6", "S7", "S8"):
        assert statuses[stage_id] == "skipped"

    rows = decisions_dao.list_for_project(session, project.id)
    assert any("跳过后续" in r.decision for r in rows)


def test_budget_exceeded_pauses_run_and_creates_approval(session, gateway):
    """US-205：Agent 级步数熔断 → run 暂停 + 审批请求。"""
    from app.store.dao import agents as agents_dao
    from app.store.dao import approvals as approvals_dao

    registry = StageRegistry()
    register_all(registry)
    orchestrator = Orchestrator(registry, gateway)
    project = projects_dao.create(session, title="预算项目", goal="g")
    agents_dao.upsert(session, agent_id="scout", name="Scout", tier="plan",
                      budget_steps=0, budget_cost=100.0)
    session.flush()

    run_id = orchestrator.run_stage(session, project.id, "S1")
    run = runs_dao.get_run(session, run_id)
    assert run.status == "paused"

    pending = approvals_dao.list_by_status(session, project_id=project.id)
    assert len(pending) == 1
    assert pending[0].kind == "budget"
    assert pending[0].detail["source"] == "agent_steps"
    assert pending[0].run_id == run_id
