"""``GET /api/tools`` 的 HTTP 契约（US-404）。

界面侧栏用它渲染工具清单与权限徽标。这里要钉住的是**界面显示的权限等级与执行时
判定的权限等级是同一个值** —— 两处各存一份，迟早出现「界面写着只读、实际弹审批」，
而那种不一致会直接摧毁用户对权限系统的信任。

另外钉住装配期的一个真实失败模式：注册表没装好时**返回 503 而不是空列表**。
空列表会渲染成「这个系统没有任何工具」，把调用方指向完全错误的方向。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store.dao import agents as agents_dao


@pytest.fixture
def client(engine, session_factory, ai_config):
    app = create_app()
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app) as c:
        yield c


def _tools(client) -> dict[str, dict]:
    resp = client.get("/api/tools")
    assert resp.status_code == 200, resp.text
    return {row["name"]: row for row in resp.json()}


# ── 正常路径 ────────────────────────────────────

def test_lists_run_pipeline_with_full_contract(client):
    row = _tools(client)["run_pipeline"]
    assert set(row) == {
        "name", "description", "parameters", "permission",
        "idempotent", "timeout_s", "result_max_bytes", "allowed_agents",
    }
    assert row["permission"] == "execute"
    assert row["idempotent"] is False
    assert row["result_max_bytes"] == 32 * 1024
    assert row["description"]


def test_parameters_are_the_schema_given_to_the_model(client):
    """``parameters`` 直接就是下发给模型的 JSON Schema —— 不是给人看的摘要。"""
    params = _tools(client)["run_pipeline"]["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["stage_ids"]
    assert params["properties"]["stage_ids"]["items"]["enum"] == [
        "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8",
    ]


def test_allowed_agents_comes_from_agent_specs(client):
    from app.agent_kernel import specs

    row = _tools(client)["run_pipeline"]
    # 8 个阶段 Agent + 会话内核（US-405）。会话内核也在内是**对的**：
    # 它确实能调 run_pipeline（衔接约定 1），漏掉它这一栏就是在说一件不成立的事。
    assert row["allowed_agents"] == [spec.id for spec in specs.ALL_AGENT_SPECS]
    assert len(row["allowed_agents"]) == 9


# ── 装配期守卫 ──────────────────────────────────

def test_returns_503_when_registry_was_never_assembled(engine, session_factory):
    """不进 ``with`` 就不跑 lifespan，正好模拟「启动流程没走完」。"""
    app = create_app()
    app.state.engine = engine
    app.state.session_factory = session_factory
    resp = TestClient(app).get("/api/tools")
    assert resp.status_code == 503
    assert "注册表" in resp.json()["detail"]


# ── 播种：agents.tools 从死列变成活列 ───────────

def test_startup_seeds_agent_whitelists_from_specs(client, session):
    """US-404 之前 ``agents.tools`` 没有任何读取点。

    这条断言落在这里（而不是 DAO 测试里）是因为它证明的是**启动路径真的在播种**：
    只测 ``upsert`` 的话，「main 忘了传 tools」仍是绿的。
    """
    from app.agent_kernel import specs

    rows = {row.agent_id: row for row in agents_dao.list_all(session)}
    # 8 个阶段 Agent + 1 个会话内核（US-405 播种，否则会话路径上 Agent 级
    # 步数/成本闸门会因为查不到 agents 行而静默失效）
    assert len(rows) == 9
    for spec in specs.STAGE_AGENT_SPECS:
        assert rows[spec.id].tools == list(spec.tools), spec.id
        assert rows[spec.id].budget_steps == spec.max_steps
        assert rows[spec.id].budget_cost == spec.max_cost_usd
    # US-406：``executor`` 白名单里多了沙箱工具，其余七个**仍然只有** run_pipeline。
    # 两条都要断：只断前者的话，「顺手给所有 Agent 都加上」照样绿。
    assert set(rows["executor"].tools) > {"run_pipeline"}
    for spec in specs.STAGE_AGENT_SPECS:
        if spec.id != "executor":
            assert rows[spec.id].tools == ["run_pipeline"], spec.id
    kernel = rows[specs.CONVERSATION_SPEC.id]
    assert kernel.tools == list(specs.CONVERSATION_SPEC.tools)
    assert kernel.budget_steps == specs.CONVERSATION_SPEC.max_steps
    assert kernel.role == "kernel"
