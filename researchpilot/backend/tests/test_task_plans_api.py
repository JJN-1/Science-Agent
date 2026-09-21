"""结构化任务计划（US-403）—— DAO 契约 + HTTP 契约。

为什么这份测试值得单独存在（而不是并进 ``test_planner.py``）：``planner`` 测的是
「计划本身对不对」，这里测的是**状态机与 HTTP 语义**。两者的失败方式完全不同 ——
planner 错了会产出坏计划，而这里错了会**改坏已经跑过的计划**，且不报错。

三条主线：

- **批准即冻结**：批准后的计划不可再改内容（否则执行中的检查点指不回当时那份）
- **确定性计划不可改步骤，但可改标题**：前者破坏可复现，后者不影响执行
- **省略步骤状态 ≠ 重置为 pending**：编辑计划不该顺手抹掉已有进度
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent_kernel import planner
from app.main import create_app
from app.store.dao import conversations as conversations_dao
from app.store.dao import projects as projects_dao
from app.store.dao import task_plans as task_plans_dao


@pytest.fixture
def client(engine, session_factory, ai_config):
    app = create_app()
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(session):
    created = projects_dao.create(session, title="计划测试", domain="cs-ai", goal="G")
    session.commit()
    return created


@pytest.fixture
def conversation(client):
    project = client.post("/api/projects", json={"title": "P"}).json()
    return client.post(
        "/api/conversations", json={"project_id": project["id"]},
    ).json()


def _new_plan(client, conversation, **payload):
    resp = client.post(
        f"/api/conversations/{conversation['id']}/task-plans", json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── DAO 契约 ────────────────────────────────────

def test_current_plan_is_the_newest_row(session, project):
    """重规划会产生新行，「当前计划」= 列表第一条 —— 不是某个 status 挑出来的。"""
    conversation = conversations_dao.create(session, project_id=project.id)
    first = task_plans_dao.create(
        session, conversation_id=conversation.id, steps=[{"id": "a", "title": "A"}],
        mode="plan_execute", deterministic=False, seed=None,
    )
    second = task_plans_dao.create(
        session, conversation_id=conversation.id, steps=[{"id": "b", "title": "B"}],
        mode="plan_execute", deterministic=False, seed=None,
    )
    session.commit()

    assert task_plans_dao.current_for_conversation(session, conversation.id).id == second.id
    assert [p.id for p in task_plans_dao.list_for_conversation(session, conversation.id)] == (
        [second.id, first.id]
    )


def test_has_running_plan_only_sees_executing(session, project):
    conversation = conversations_dao.create(session, project_id=project.id)
    plan = task_plans_dao.create(
        session, conversation_id=conversation.id, steps=[{"id": "a", "title": "A"}],
        mode="plan_execute", deterministic=False, seed=None,
    )
    session.commit()
    assert task_plans_dao.has_running_plan(session, conversation.id) is False

    task_plans_dao.set_status(session, plan.id, "executing")
    session.commit()
    assert task_plans_dao.has_running_plan(session, conversation.id) is True

    task_plans_dao.set_status(session, plan.id, "done")
    session.commit()
    assert task_plans_dao.has_running_plan(session, conversation.id) is False


def test_update_content_bumps_version(session, project):
    conversation = conversations_dao.create(session, project_id=project.id)
    plan = task_plans_dao.create(
        session, conversation_id=conversation.id, steps=[{"id": "a", "title": "A"}],
        mode="plan_execute", deterministic=False, seed=None,
    )
    session.commit()
    assert plan.version == 1

    task_plans_dao.update_content(session, plan.id, title="改过的标题")
    session.commit()

    assert plan.version == 2
    assert plan.title == "改过的标题"
    assert task_plans_dao.update_content(session, 999, title="x") is None
    assert task_plans_dao.set_status(session, 999, "done") is None


# ── 生成 ────────────────────────────────────────

def test_create_plan_defaults_to_the_research_pipeline_template(client, conversation):
    body = _new_plan(client, conversation, goal="图神经网络的过平滑")

    assert body["status"] == "draft"
    assert body["version"] == 1
    assert body["mode"] == "plan_execute"
    assert body["deterministic"] is False
    assert body["seed"] is None
    assert [s["id"] for s in body["steps"]] == [
        s.id for s in planner.RESEARCH_PIPELINE.steps
    ]
    # 每个阶段步骤都指向 run_pipeline（衔接约定 1 的可断言形式）
    assert all(s["tool"] == "run_pipeline" for s in body["steps"])
    assert "图神经网络的过平滑" in body["rationale"]


def test_deterministic_plan_records_seed_and_says_so(client, conversation):
    body = _new_plan(client, conversation, goal="目标", deterministic=True)

    assert body["deterministic"] is True
    assert body["seed"] == planner.DETERMINISTIC_SEED
    assert "确定性模式" in body["rationale"]


def test_two_deterministic_plans_have_identical_step_payloads(client, conversation):
    """G2 第 7 条走接口这条路也要成立：两次请求 → 步骤序列逐项相等。"""
    first = _new_plan(client, conversation, goal="同一目标", deterministic=True)
    second = _new_plan(client, conversation, goal="同一目标", deterministic=True)

    # id 与 version 当然不同（各是一行），但计划内容必须一致
    assert first["steps"] == second["steps"]
    assert first["seed"] == second["seed"]


def test_create_plan_for_unknown_conversation_is_404(client):
    assert client.post("/api/conversations/999/task-plans", json={}).status_code == 404


def test_create_plan_rejects_react_mode_with_a_reason(client, conversation):
    """react 与模板互斥，必须是 400 且说清原因 —— 不能悄悄给一份 plan_execute 计划。"""
    resp = client.post(
        f"/api/conversations/{conversation['id']}/task-plans", json={"mode": "react"},
    )
    assert resp.status_code == 400
    assert "react" in resp.json()["detail"]


def test_create_plan_rejects_unknown_mode_and_template(client, conversation):
    for payload in ({"mode": "group_chat"}, {"template_id": "nope"}):
        resp = client.post(
            f"/api/conversations/{conversation['id']}/task-plans", json=payload,
        )
        assert resp.status_code == 400, payload


def test_create_plan_is_refused_while_a_plan_is_executing(client, conversation, session):
    """执行期间重规划会让「当前计划」有两个答案，所以直接拒。"""
    plan = _new_plan(client, conversation)
    task_plans_dao.set_status(session, plan["id"], "executing")
    session.commit()

    resp = client.post(
        f"/api/conversations/{conversation['id']}/task-plans", json={},
    )
    assert resp.status_code == 409
    assert "执行" in resp.json()["detail"]


# ── 查询 ────────────────────────────────────────

def test_list_returns_newest_first_and_get_single(client, conversation):
    first = _new_plan(client, conversation, goal="第一版")
    second = _new_plan(client, conversation, goal="第二版")

    listed = client.get(f"/api/conversations/{conversation['id']}/task-plans").json()
    assert [p["id"] for p in listed] == [second["id"], first["id"]]

    single = client.get(f"/api/task-plans/{first['id']}")
    assert single.status_code == 200
    assert single.json()["id"] == first["id"]
    assert client.get("/api/task-plans/999").status_code == 404
    assert client.get("/api/conversations/999/task-plans").status_code == 404


# ── 修改与批准 ──────────────────────────────────

def test_content_edit_bumps_version_and_replaces_steps(client, conversation):
    plan = _new_plan(client, conversation)
    resp = client.patch(
        f"/api/task-plans/{plan['id']}",
        json={
            "title": "精简版",
            "steps": [{"id": "only", "title": "只做一件事"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 2
    assert body["title"] == "精简版"
    assert [s["id"] for s in body["steps"]] == ["only"]


def test_approving_freezes_the_plan(client, conversation):
    plan = _new_plan(client, conversation)

    approved = client.patch(f"/api/task-plans/{plan['id']}", json={"status": "approved"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    # 已批准 → 改内容被拒（409 是「这次操作本身不该发生」，不是请求体写错了）
    frozen = client.patch(
        f"/api/task-plans/{plan['id']}", json={"title": "批准后还想改"},
    )
    assert frozen.status_code == 409
    assert client.get(f"/api/task-plans/{plan['id']}").json()["title"] != "批准后还想改"

    # 再批准一次也被拒
    assert client.patch(
        f"/api/task-plans/{plan['id']}", json={"status": "approved"},
    ).status_code == 409


def test_editing_keeps_the_status_of_steps_that_omit_it(client, conversation, session):
    """省略 ``status`` 是「沿用」，不是「重置为 pending」—— 编辑计划不该抹掉进度。"""
    plan = _new_plan(client, conversation)
    steps = plan["steps"]
    steps[0]["status"] = "done"
    steps[1]["status"] = "running"
    task_plans_dao.update_content(session, plan["id"], steps=steps)
    session.commit()

    resp = client.patch(
        f"/api/task-plans/{plan['id']}",
        json={"steps": [{"id": s["id"], "title": s["title"]} for s in steps]},
    )
    assert resp.status_code == 200
    got = resp.json()["steps"]
    assert got[0]["status"] == "done"
    assert got[1]["status"] == "running"
    assert got[2]["status"] == "pending"


def test_new_step_in_an_edit_starts_pending(client, conversation):
    """新出现的步骤没有历史状态可沿用 —— 只能是 pending。"""
    plan = _new_plan(client, conversation)
    resp = client.patch(
        f"/api/task-plans/{plan['id']}",
        json={"steps": [{"id": "brand-new", "title": "新步骤"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["steps"][0]["status"] == "pending"


def test_deterministic_plan_forbids_step_edits_but_allows_a_rename(client, conversation):
    """D7：确定性计划的步骤序列是它的全部价值；标题不进执行，改了不影响可复现。"""
    plan = _new_plan(client, conversation, deterministic=True)

    blocked = client.patch(
        f"/api/task-plans/{plan['id']}",
        json={"steps": [{"id": "x", "title": "偷改一步"}]},
    )
    assert blocked.status_code == 409
    assert "确定性" in blocked.json()["detail"]

    renamed = client.patch(f"/api/task-plans/{plan['id']}", json={"title": "换个名字"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "换个名字"
    assert renamed.json()["steps"] == plan["steps"]


def test_bad_edits_are_400(client, conversation):
    plan = _new_plan(client, conversation)
    url = f"/api/task-plans/{plan['id']}"

    # 空步骤：计划至少要一步
    assert client.patch(url, json={"steps": []}).status_code == 400
    # 重复 id：检查点无法确定从哪恢复
    assert client.patch(
        url,
        json={"steps": [{"id": "s", "title": "一"}, {"id": "s", "title": "二"}]},
    ).status_code == 400
    # 步骤状态非法
    assert client.patch(
        url, json={"steps": [{"id": "s", "title": "t", "status": "bogus"}]},
    ).status_code == 400
    # 该接口不接受 approved 之外的 status
    resp = client.patch(url, json={"status": "executing"})
    assert resp.status_code == 400
    assert "approved" in resp.json()["detail"]
    # 标题超长
    assert client.patch(url, json={"title": "x" * 256}).status_code == 422

    assert client.patch("/api/task-plans/999", json={"title": "x"}).status_code == 404
