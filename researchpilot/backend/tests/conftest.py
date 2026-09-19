from __future__ import annotations

import time

import pytest

from app.store.db import make_engine, make_session_factory
from app.store.migrations import upgrade_to_head

# 与 default.yaml 一致的 mock 响应（S1 结构化输出）
MOCK_RESPONSE = (
    '{"questions": [{"question": "Q1", "rationale": "R1", "score": 0.8},'
    ' {"question": "Q2", "rationale": "R2", "score": 0.7}]}'
)


@pytest.fixture
def ai_config() -> dict:
    return {
        "ai": {
            "providers": {
                "mock": {
                    "type": "mock", "model": "mock-small", "vendor": "mock",
                    "capabilities": ["json_object"],
                    "price_per_1k": {"input": 0.01, "output": 0.03},
                    "response": MOCK_RESPONSE,
                }
            },
            "routing": {
                "extract": [{"provider": "mock", "model": "mock-small"}],
                "plan": [{"provider": "mock", "model": "mock-small"}],
                "critique": [{"provider": "mock", "model": "mock-small"}],
                "synthesize": [{"provider": "mock", "model": "mock-small"}],
                "write": [{"provider": "mock", "model": "mock-small"}],
            },
            "budget": {"project_total": 50.0, "project_daily": 10.0},
            "circuit": {"failure_threshold": 5, "cooldown_seconds": 300},
        }
    }


@pytest.fixture
def gateway(ai_config):
    from app.ai.budget import BudgetManager
    from app.ai.client import LlmGateway
    from app.ai.registry import ProviderRegistry
    from app.ai.routing import Router

    registry = ProviderRegistry.from_config(ai_config)
    router = Router.from_config(ai_config, registry.providers_map())
    return LlmGateway(registry, router, BudgetManager(ai_config["ai"]["budget"]))


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("RESEARCHPILOT_DATA_DIR", str(root))
    return root


@pytest.fixture
def engine(data_root):
    engine = make_engine(data_root / "app.db")
    upgrade_to_head(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def session(session_factory):
    s = session_factory()
    yield s
    s.rollback()
    s.close()


TERMINAL_JOB_STATUSES = ("succeeded", "failed", "paused")


def _await_job(client, job_id: int, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/jobs/{job_id}").json()
        if snapshot["status"] in TERMINAL_JOB_STATUSES:
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"作业 {job_id} 未在 {timeout}s 内结束：{snapshot}")


@pytest.fixture
def wait_job():
    """等作业到终态后返回快照。

    FIX-03 之后 ``POST .../run`` 只受理，测试必须自己等 —— 这正是「受理与执行
    解耦」在测试侧的表现。
    """
    def _wait(client, job_id: int, timeout: float = 20.0) -> dict:
        return _await_job(client, job_id, timeout)

    return _wait


@pytest.fixture
def run_and_wait():
    """受理一个阶段运行并等它结束，返回 ``(job_id, 快照)``。"""
    def _run(client, project_id: int, stage_id: str,
             timeout: float = 20.0) -> tuple[int, dict]:
        resp = client.post(f"/api/projects/{project_id}/stages/{stage_id}/run")
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        return job_id, _await_job(client, job_id, timeout)

    return _run
