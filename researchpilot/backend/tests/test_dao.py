from __future__ import annotations

from app.store.dao import agents as agents_dao
from app.store.dao import blackboard as bb_dao
from app.store.dao import projects as projects_dao
from app.store.dao import runs as runs_dao


def test_project_crud(session):
    project = projects_dao.create(session, title="图神经网络研究", goal="验证 GNN 在推荐系统的效果")
    assert project.id is not None
    assert project.status == "created"

    fetched = projects_dao.get(session, project.id)
    assert fetched is not None and fetched.title == "图神经网络研究"

    projects_dao.update(session, project.id, status="running")
    assert projects_dao.get(session, project.id).status == "running"

    assert len(projects_dao.list_all(session)) == 1


def test_blackboard_version_increments_per_type(session):
    project = projects_dao.create(session, title="p")
    first = bb_dao.write(session, project_id=project.id, obj_type="hypothesis",
                         payload={"text": "H1"}, produced_by="formalizer")
    second = bb_dao.write(session, project_id=project.id, obj_type="hypothesis",
                          payload={"text": "H2"}, produced_by="formalizer")
    other = bb_dao.write(session, project_id=project.id, obj_type="stage_output",
                         payload={}, produced_by="scout")
    assert (first.version, second.version, other.version) == (1, 2, 1)

    latest = bb_dao.latest_by_type(session, project.id)
    assert latest["hypothesis"].payload == {"text": "H2"}


def test_run_steps_sequence_and_finish(session):
    project = projects_dao.create(session, title="p")
    run = runs_dao.create_run(session, project_id=project.id, stage_id="S1", agent_id="scout")
    s1 = runs_dao.add_step(session, run_id=run.id, kind="thought", content={"text": "t1"})
    s2 = runs_dao.add_step(session, run_id=run.id, kind="result", content={"text": "r1"})
    assert (s1.seq, s2.seq) == (1, 2)

    runs_dao.finish_run(session, run_id=run.id, status="succeeded")
    reloaded = runs_dao.get_run(session, run.id)
    assert reloaded.status == "succeeded"
    assert reloaded.steps == 2
    assert reloaded.finished_at is not None
    assert [s.kind for s in runs_dao.list_steps(session, run.id)] == ["thought", "result"]


# ── agents.upsert 的 None 语义（US-404）──────────
# `agents.tools` 在 US-404 之前是一列死数据。它一开始由 AgentSpec 播种，就必须
# **连已存在的行也更新**（只更新插入路径 = 老安装永远拿不到白名单）。

def test_agents_upsert_updates_contract_fields_on_existing_row(session):
    created = agents_dao.upsert(session, "scout", "Scout", tier="plan")
    session.flush()
    assert created.tools == []

    agents_dao.upsert(
        session, "scout", "Scout", tier="plan",
        tools=["run_pipeline"], budget_steps=20, budget_cost=2.0,
    )
    session.flush()
    reloaded = agents_dao.get_by_agent_id(session, "scout")
    assert reloaded.tools == ["run_pipeline"]
    assert reloaded.id == created.id      # 更新而不是插了一行新的


def test_agents_upsert_none_means_leave_untouched(session):
    """``None`` = 本次不动这个字段，不是「用默认值覆盖」。

    没有这条语义，任何一次「只改名字」的播种都会把用户/别处设好的白名单与预算抹掉，
    而且不报错。
    """
    agents_dao.upsert(
        session, "scout", "Scout", tier="plan",
        tools=["run_pipeline"], budget_steps=7, budget_cost=1.5,
    )
    session.flush()

    agents_dao.upsert(session, "scout", "Scout 改名", tier="extract")
    session.flush()

    reloaded = agents_dao.get_by_agent_id(session, "scout")
    assert reloaded.name == "Scout 改名" and reloaded.tier == "extract"
    assert reloaded.tools == ["run_pipeline"]
    assert reloaded.budget_steps == 7 and reloaded.budget_cost == 1.5


def test_agents_upsert_zero_budget_is_a_real_value(session):
    """``budget_steps=0`` 是合法值（测试用它模拟「一步就熔断」）。

    判据必须是 ``is not None`` 而不是真值判断 —— 写成 ``if budget_steps:`` 时，
    **把预算调到 0 会被静默忽略**，表现为「明明设了 0，却还在继续跑」。
    必须先把行建出来再改，才能走到更新路径：只建一行的话，真值判断在新插入路径上
    也恰好得到 0（`budget_steps=0 if ... else 20`），测试会绿着漏掉这个 bug。
    """
    agents_dao.upsert(session, "scout", "Scout", tier="plan", budget_steps=5, budget_cost=1.0)
    session.flush()
    assert agents_dao.get_by_agent_id(session, "scout").budget_steps == 5

    agents_dao.upsert(session, "scout", "Scout", tier="plan", budget_steps=0, budget_cost=0.0)
    session.flush()
    reloaded = agents_dao.get_by_agent_id(session, "scout")
    assert reloaded.budget_steps == 0
    assert reloaded.budget_cost == 0.0

