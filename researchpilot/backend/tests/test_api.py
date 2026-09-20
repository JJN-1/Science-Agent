from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


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


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_stages_expose_implementation_marker(client):
    """US-307：/api/stages 必须暴露 implemented / planned_sprint，
    否则前端无法区分「真实阶段」与「占位阶段」，会把占位结果当成正式产出。
    """
    stages = client.get("/api/stages").json()
    assert len(stages) == 8

    by_id = {s["stage_id"]: s for s in stages}
    assert by_id["S1"]["implemented"] is True
    assert by_id["S1"]["planned_sprint"] is None

    for stage_id, planned in [("S2", 6), ("S3", 7), ("S4", 8),
                              ("S5", 9), ("S6", 10), ("S7", 11), ("S8", 11)]:
        assert by_id[stage_id]["implemented"] is False, stage_id
        assert by_id[stage_id]["planned_sprint"] == planned, stage_id


def test_project_stage_run_trajectory_flow(client, run_and_wait):
    created = client.post("/api/projects", json={"title": "接口演示", "goal": "G"})
    assert created.status_code == 201
    project_id = created.json()["id"]

    stages = client.get("/api/stages").json()
    assert [s["stage_id"] for s in stages] == [f"S{i}" for i in range(1, 9)]

    _, snapshot = run_and_wait(client, project_id, "S2")
    assert snapshot["status"] == "succeeded"
    run_id = snapshot["run_id"]

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "succeeded"
    assert detail["stage_id"] == "S2"
    assert len(detail["steps"]) == 2

    runs = client.get(f"/api/projects/{project_id}/runs").json()
    assert len(runs) == 1

    board = client.get(f"/api/projects/{project_id}/blackboard").json()
    assert board[0]["obj_type"] == "stage_output"
    assert board[0]["version"] == 1

    assert client.post(f"/api/projects/{project_id}/stages/S99/run").status_code == 404
    assert client.get("/api/runs/9999").status_code == 404


def test_failed_stage_is_persisted_with_actionable_error(client, run_and_wait):
    """失败的 run / checkpoint / failed_attempt 必须落库。

    修复前：get_session 在任何异常上统一 rollback，把 Orchestrator 刚写好的失败现场
    一并丢弃，前端「详见流内记录」指向空——最该留的失败轨迹反而没有。
    现在这段责任在 worker 身上（受理接口手上没有可失败的阶段了）。
    """
    project_id = client.post(
        "/api/projects", json={"title": "失败落库", "goal": "G"}
    ).json()["id"]
    client.app.state.ai_registry.get("mock")._fail_times = 999  # 模型持续不可用

    _, snapshot = run_and_wait(client, project_id, "S1")
    assert snapshot["status"] == "failed"
    assert "unavailable" in (snapshot["error"] or "")

    runs = client.get(f"/api/projects/{project_id}/runs").json()
    assert len(runs) == 1 and runs[0]["status"] == "failed"
    assert runs[0]["id"] == snapshot["run_id"]  # 失败作业仍指向失败现场

    detail = client.get(f"/api/runs/{runs[0]['id']}").json()
    assert "unavailable" in (detail["error"] or "")

    assert [c["status"] for c in
            client.get(f"/api/projects/{project_id}/checkpoints").json()] == ["failed"]
    assert [d["kind"] for d in
            client.get(f"/api/projects/{project_id}/decisions").json()] == ["failed_attempt"]

    # 失败的调用同样要留痕（用户投诉的「供应商统计里没有这条请求」）
    usage = client.get(f"/api/usage/summary?project_id={project_id}&dim=provider").json()
    assert len(usage["rows"]) == 1
    assert usage["rows"][0] == {"key": "mock", "calls": 1, "failed": 1,
                               "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    assert usage["total_cost"] == 0.0


def test_unparseable_output_is_kept_in_trajectory(client, run_and_wait):
    """FIX-06：解析失败时原始输出必须留在轨迹里，否则无从排查模型到底吐了什么。

    形状校验上线后，这条路径**不再**由 S1 自己解析时发现，而是被网关的
    ``LLM-SCHEMA-001`` 拦下——原始输出得由 ``StageContext.llm`` 的失败分支补记。
    断言不变，说明「原文必须进轨迹」的保证对新的失败位置同样成立。
    """
    project_id = client.post(
        "/api/projects", json={"title": "输出解析", "goal": "G"}
    ).json()["id"]
    # 合法 JSON 但不是对象 → 两次形状校验都不过，原始输出仍必须完整落轨迹
    client.app.state.ai_registry.get("mock")._response = "[1, 2, 3]"

    _, snapshot = run_and_wait(client, project_id, "S1")
    assert snapshot["status"] == "failed"

    runs = client.get(f"/api/projects/{project_id}/runs").json()
    detail = client.get(f"/api/runs/{runs[0]['id']}").json()
    errors = [s for s in detail["steps"] if s["kind"] == "error"]
    assert len(errors) == 1
    assert errors[0]["content"]["raw"] == "[1, 2, 3]"


def test_shape_violation_fails_stage_instead_of_succeeding_empty(client, run_and_wait):
    """止血回归：模型回「合法 JSON 但形状不对」必须让阶段**失败**，不能空成功。

    真实事故（用户报的「跑了几分钟、显示成功、一个结果都没有」）：`response_format=
    json_object` 只保证「是 JSON」，不保证形状；某个后端于是回了

        {"response": "……我生成了 3 个候选研究问题……", "format": "JSON", "note": "……"}

    旧实现 `parsed.get("questions", [])` 拿到空列表照样写黑板、报 ``stage.succeeded``。
    现在 schema 始终随 prompt 下发，形状违规两次即失败，并明确指出违规点。
    """
    project_id = client.post(
        "/api/projects", json={"title": "形状违规", "goal": "G"}
    ).json()["id"]
    client.app.state.ai_registry.get("mock")._response = (
        '{"response": "我已生成 3 个候选研究问题", "format": "JSON"}'
    )

    _, snapshot = run_and_wait(client, project_id, "S1")
    assert snapshot["status"] == "failed"
    assert "LLM-SCHEMA-001" in (snapshot["error"] or "")

    # 关键：绝不能留下「空但成功」的黑板对象 —— 那正是用户看到「没结果」的来源
    assert client.get(f"/api/projects/{project_id}/blackboard").json() == []


def test_lifespan_wires_the_async_job_layer(client, run_and_wait):
    """FIX-03：应用启动即挂上作业层，worker 真的会在后台把作业跑到终态。"""
    assert client.app.state.job_runner is not None
    project_id = client.post(
        "/api/projects", json={"title": "异步冒烟", "goal": "图神经网络推荐"}
    ).json()["id"]

    job_id, snapshot = run_and_wait(client, project_id, "S1")
    assert snapshot["status"] == "succeeded", snapshot["error"]

    events = client.get(f"/api/jobs/{job_id}/events").json()
    assert events[0]["type"] == "job.queued"
    assert events[-1]["type"] == "job.succeeded"
