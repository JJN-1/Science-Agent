from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(engine, session_factory):
    app = create_app()
    app.state.engine = engine
    app.state.session_factory = session_factory
    from app.agents.demo_stage import register_all
    from app.orchestration.orchestrator import Orchestrator, StageRegistry

    registry = StageRegistry()
    register_all(registry)
    app.state.orchestrator = Orchestrator(registry)
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
