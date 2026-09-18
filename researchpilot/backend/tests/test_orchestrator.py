from __future__ import annotations

import pytest

from app.agents.demo_stage import register_all
from app.orchestration.base import StageAgent
from app.orchestration.orchestrator import STAGE_ORDER, Orchestrator, StageRegistry
from app.store.dao import blackboard as bb_dao
from app.store.dao import checkpoints as cp_dao
from app.store.dao import projects as projects_dao
from app.store.dao import runs as runs_dao


@pytest.fixture
def orchestrator():
    registry = StageRegistry()
    register_all(registry)
    return Orchestrator(registry)


def test_registry_rejects_duplicate_stage():
    registry = StageRegistry()
    register_all(registry)
    duplicate = type(registry.get("S1"))("S1", "scout", "重复", "")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(duplicate)


def test_run_stage_records_trajectory_checkpoint_and_blackboard(session, orchestrator):
    project = projects_dao.create(session, title="演示项目")
    run_id = orchestrator.run_stage(session, project.id, "S1")

    run = runs_dao.get_run(session, run_id)
    assert run.status == "succeeded"
    assert run.agent_id == "scout"
    steps = runs_dao.list_steps(session, run_id)
    assert [s.kind for s in steps] == ["thought", "decision"]

    checkpoints = cp_dao.list_for_project(session, project.id)
    assert len(checkpoints) == 1 and checkpoints[0].stage_id == "S1"

    outputs = [o for o in bb_dao.list_for_project(session, project.id) if o.obj_type == "stage_output"]
    assert len(outputs) == 1
    assert outputs[0].produced_by == "scout"
    assert outputs[0].payload["stage_id"] == "S1"


def test_run_stage_failure_marks_run_failed_without_success_checkpoint(session):
    class FailingStage(StageAgent):
        stage_id, agent_id, name = "SX", "tester", "失败阶段"

        def run(self, ctx):
            ctx.think("即将失败")
            raise RuntimeError("boom")

    registry = StageRegistry()
    registry.register(FailingStage())
    orchestrator = Orchestrator(registry)
    project = projects_dao.create(session, title="p")

    with pytest.raises(RuntimeError, match="boom"):
        orchestrator.run_stage(session, project.id, "SX")

    run = runs_dao.list_for_project(session, project.id)[0]
    assert run.status == "failed" and "boom" in run.error
    checkpoints = cp_dao.list_for_project(session, project.id)
    assert checkpoints[0].status == "failed"


def test_run_pipeline_executes_in_order(session, orchestrator):
    project = projects_dao.create(session, title="全链路")
    run_ids = orchestrator.run_pipeline(session, project.id)
    assert len(run_ids) == 8
    stages_run = [r.stage_id for r in runs_dao.list_for_project(session, project.id)]
    assert stages_run == list(reversed(STAGE_ORDER))  # list_for_project 按新→旧排序
    assert len(cp_dao.list_for_project(session, project.id)) == 8
    # 同类型黑板对象逐阶段版本递增
    versions = [o.version for o in bb_dao.list_for_project(session, project.id)]
    assert versions == list(range(1, 9))
