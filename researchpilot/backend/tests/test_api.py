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


def test_project_stage_run_trajectory_flow(client):
    created = client.post("/api/projects", json={"title": "接口演示", "goal": "G"})
    assert created.status_code == 201
    project_id = created.json()["id"]

    stages = client.get("/api/stages").json()
    assert [s["stage_id"] for s in stages] == [f"S{i}" for i in range(1, 9)]

    run_resp = client.post(f"/api/projects/{project_id}/stages/S2/run")
    assert run_resp.status_code == 200
    run_id = run_resp.json()["run_id"]

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


def test_failed_stage_is_persisted_with_actionable_error(client):
    """失败的 run / checkpoint / failed_attempt 必须落库。

    修复前：get_session 在任何异常上统一 rollback，把 Orchestrator 刚写好的失败现场
    一并丢弃，前端「详见流内记录」指向空——最该留的失败轨迹反而没有。
    """
    project_id = client.post(
        "/api/projects", json={"title": "失败落库", "goal": "G"}
    ).json()["id"]
    client.app.state.ai_registry.get("mock")._fail_times = 999  # 模型持续不可用

    resp = client.post(f"/api/projects/{project_id}/stages/S1/run")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "LLM-UNAVAIL-001"

    runs = client.get(f"/api/projects/{project_id}/runs").json()
    assert len(runs) == 1 and runs[0]["status"] == "failed"

    detail = client.get(f"/api/runs/{runs[0]['id']}").json()
    assert "unavailable" in (detail["error"] or "")

    assert [c["status"] for c in
            client.get(f"/api/projects/{project_id}/checkpoints").json()] == ["failed"]
    assert [d["kind"] for d in
            client.get(f"/api/projects/{project_id}/decisions").json()] == ["failed_attempt"]


def test_unparseable_output_is_kept_in_trajectory(client):
    """FIX-06：解析失败时原始输出必须留在轨迹里，否则无从排查模型到底吐了什么。"""
    project_id = client.post(
        "/api/projects", json={"title": "输出解析", "goal": "G"}
    ).json()["id"]
    # 合法 JSON 但不是对象 → 通过 schema 校验却在 S1 解析阶段失败，正好命中该分支
    client.app.state.ai_registry.get("mock")._response = "[1, 2, 3]"

    assert client.post(f"/api/projects/{project_id}/stages/S1/run").status_code == 500

    runs = client.get(f"/api/projects/{project_id}/runs").json()
    detail = client.get(f"/api/runs/{runs[0]['id']}").json()
    errors = [s for s in detail["steps"] if s["kind"] == "error"]
    assert len(errors) == 1
    assert errors[0]["content"]["raw"] == "[1, 2, 3]"
