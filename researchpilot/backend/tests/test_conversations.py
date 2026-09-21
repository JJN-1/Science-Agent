"""会话与消息（US-401）与上下文预览接口（US-402）。

两条主线：
- **持久化的契约**：会话归档不删数据、追加消息会把会话推新、限流取的是最近 N 条
- **接口的契约**：`POST /messages` 的 `job_id` 在第 1 步必须是 `null`（D14）——
  返回一个假 id 会让前端去订阅一个不存在的作业，而返回 null 是诚实的
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store.dao import conversations as conversations_dao
from app.store.dao import messages as messages_dao
from app.store.dao import projects as projects_dao


@pytest.fixture
def client(engine, session_factory, ai_config):
    """这些接口只依赖 session_factory；AI/编排不需要。

    lifespan 会自己建好 ``app.state``（与 test_api.py 同一条路径，
    因为 RESEARCHPILOT_DATA_DIR 指向同一个 tmp 目录）。
    """
    app = create_app()
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(session):
    created = projects_dao.create(session, title="会话测试", domain="cs-ai", goal="G")
    session.commit()
    return created


# ── DAO 契约 ────────────────────────────────────

def test_archive_hides_from_default_list_without_deleting(session, project):
    """归档是「从默认视野里消失」，不是删除 —— 数据必须还在。"""
    kept = conversations_dao.create(session, project_id=project.id, title="保留")
    gone = conversations_dao.create(session, project_id=project.id, title="归档")
    conversations_dao.archive(session, gone.id)
    session.commit()

    default = conversations_dao.list_for_project(session, project.id)
    assert [c.id for c in default] == [kept.id]

    everything = conversations_dao.list_for_project(
        session, project.id, include_archived=True,
    )
    assert {c.id for c in everything} == {kept.id, gone.id}
    assert conversations_dao.get(session, gone.id) is not None


def test_appending_a_message_touches_the_conversation(session, project):
    """会话列表按 updated_at 排序，而追加消息动的是 messages 表 ——
    ORM 的 onupdate 不会触发，所以这条测试守的是「有人记得显式推一下」。
    """
    conversation = conversations_dao.create(session, project_id=project.id)
    conversation.updated_at = datetime(2000, 1, 1)
    session.flush()

    messages_dao.create(
        session, conversation_id=conversation.id, role="user",
        content="你好", tokens=3,
    )
    session.refresh(conversation)

    assert conversation.updated_at > datetime(2000, 1, 1)


def test_message_listing_limit_returns_the_newest_in_chronological_order(session, project):
    conversation = conversations_dao.create(session, project_id=project.id)
    for index in range(6):
        messages_dao.create(
            session, conversation_id=conversation.id, role="user",
            content=f"m{index}", tokens=1,
        )
    session.commit()

    recent = messages_dao.list_for_conversation(session, conversation.id, limit=2)
    assert [m.content for m in recent] == ["m4", "m5"]
    assert [m.content for m in messages_dao.list_for_conversation(session, conversation.id)] == (
        [f"m{i}" for i in range(6)]
    )
    assert messages_dao.count_for_conversation(session, conversation.id) == 6
    assert messages_dao.total_tokens(session, conversation.id) == 6


# ── HTTP 契约 ───────────────────────────────────

def test_create_conversation_requires_existing_project(client):
    assert client.post("/api/conversations", json={"project_id": 999}).status_code == 404

    project = client.post("/api/projects", json={"title": "P"}).json()
    created = client.post("/api/conversations", json={"project_id": project["id"]})
    assert created.status_code == 201
    assert created.json()["status"] == "active"


def test_append_message_accepts_a_chat_job(client, wait_job):
    """US-405 起 ``POST /messages`` 受理一次内核运行：202 + 真实的 ``job_id``。

    D14 的兑现方式正是「**只换值不动形状**」：响应体还是 ``{message, job_id}``，
    只是 ``job_id`` 从恒为 ``null`` 变成了 ``kind=chat`` 作业的 id ——
    前端从第 1 步起就按可空处理，所以这次改动没有破坏任何已经联调过的契约。

    状态码 201 → 202 是必须的：答复此刻**还没产生**，返回 201 会让前端以为
    「资源已就绪」，而模型一次都还没调用。
    """
    project = client.post("/api/projects", json={"title": "P"}).json()
    conversation = client.post(
        "/api/conversations", json={"project_id": project["id"]},
    ).json()

    resp = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "研究一下稀疏注意力"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["message"]["role"] == "user"
    assert body["message"]["tokens"] > 0
    assert body["job_id"] is not None

    snapshot = wait_job(client, body["job_id"])
    assert snapshot["kind"] == "chat"
    assert snapshot["status"] == "succeeded", snapshot["error"]
    assert snapshot["run_id"] is not None

    # 用户那句话必须**先落地再派活**：worker 是另一条线程、另一个 session，
    # 提交晚了它本轮就看不到这句话（表现为「模型答非所问」，且只偶发）。
    detail = client.get(f"/api/conversations/{conversation['id']}").json()
    assert detail["messages"][0]["content"] == "研究一下稀疏注意力"


def test_append_message_rejects_non_user_roles(client):
    """``role`` 只接受 ``user``。开放它等于允许客户端伪造「助手说过什么」。"""
    project = client.post("/api/projects", json={"title": "P"}).json()
    conversation = client.post(
        "/api/conversations", json={"project_id": project["id"]},
    ).json()

    resp = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "我假装是助手", "role": "assistant"},
    )
    assert resp.status_code == 400
    assert "user" in resp.json()["detail"]


def test_first_message_becomes_the_conversation_title(client):
    project = client.post("/api/projects", json={"title": "P"}).json()
    conversation = client.post(
        "/api/conversations", json={"project_id": project["id"]},
    ).json()
    client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "把图神经网络的过平滑问题做成可证伪假设"},
    )
    detail = client.get(f"/api/conversations/{conversation['id']}").json()
    assert detail["title"].startswith("把图神经网络")


def test_append_message_rejects_forged_assistant_role(client):
    """不允许客户端伪造「助手说过什么」—— 轨迹的价值在于只能由系统产生。"""
    project = client.post("/api/projects", json={"title": "P"}).json()
    conversation = client.post(
        "/api/conversations", json={"project_id": project["id"]},
    ).json()

    resp = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "我说过这句话", "role": "assistant"},
    )
    assert resp.status_code == 400
    assert "role=user" in resp.json()["detail"]


def test_detail_reports_messages_and_total_tokens(client):
    project = client.post("/api/projects", json={"title": "P"}).json()
    conversation = client.post(
        "/api/conversations", json={"project_id": project["id"]},
    ).json()
    for text in ("第一句", "第二句"):
        client.post(
            f"/api/conversations/{conversation['id']}/messages", json={"content": text},
        )

    detail = client.get(f"/api/conversations/{conversation['id']}").json()
    assert [m["content"] for m in detail["messages"]] == ["第一句", "第二句"]
    assert detail["total_tokens"] == sum(m["tokens"] for m in detail["messages"])


def test_detail_exposes_tool_calls_for_rebuilding_after_a_dropped_stream(session, client):
    """``messages.tool_calls`` 必须出到接口上（US-405）。

    终态一律以数据库重拉为准（前端既有约定）。SSE 断了之后要重建这轮对话，
    助手那句「我来读一下」就得能指出它指的是哪次调用 —— 否则后面那条 tool 消息
    挂着的 ``tool_call_id`` 找不到对手方，重建出来的轨迹里工具结果凭空出现。
    """
    project = client.post("/api/projects", json={"title": "P"}).json()
    conversation = client.post(
        "/api/conversations", json={"project_id": project["id"]},
    ).json()
    messages_dao.create(
        session, conversation_id=conversation["id"], role="assistant",
        content="我来读一下这个文件", tokens=9,
        tool_calls=[{"id": "c1", "name": "read_file", "arguments": '{"path": "a.txt"}'}],
    )
    messages_dao.create(
        session, conversation_id=conversation["id"], role="tool",
        content="文件内容", tool_call_id="c1", tokens=4,
    )
    session.commit()

    rows = client.get(f"/api/conversations/{conversation['id']}").json()["messages"]
    assert rows[0]["tool_calls"] == [
        {"id": "c1", "name": "read_file", "arguments": '{"path": "a.txt"}'},
    ], rows[0]
    assert rows[0]["tool_calls"][0]["id"] == rows[1]["tool_call_id"]
    assert rows[1]["tool_calls"] is None


def test_patch_can_archive_and_reactivate(client):
    project = client.post("/api/projects", json={"title": "P"}).json()
    conversation = client.post(
        "/api/conversations", json={"project_id": project["id"]},
    ).json()

    archived = client.patch(
        f"/api/conversations/{conversation['id']}", json={"status": "archived"},
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert client.get(f"/api/projects/{project['id']}/conversations").json() == []
    assert len(
        client.get(
            f"/api/projects/{project['id']}/conversations?include_archived=true",
        ).json()
    ) == 1

    reactivated = client.patch(
        f"/api/conversations/{conversation['id']}", json={"status": "active"},
    )
    assert reactivated.json()["status"] == "active"

    assert client.patch(
        f"/api/conversations/{conversation['id']}", json={"status": "bogus"},
    ).status_code == 400


def test_context_preview_exposes_the_trimming_ledger(client):
    """把「裁了什么」摆到接口上：只写进日志的裁剪，裁错了也没人会发现。"""
    project = client.post("/api/projects", json={"title": "P"}).json()
    conversation = client.post(
        "/api/conversations", json={"project_id": project["id"]},
    ).json()
    for index in range(6):
        client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={"content": f"第{index}轮：请分析这个问题"},
        )

    preview = client.get(
        f"/api/conversations/{conversation['id']}/context?budget_tokens=4000&recent_turns=2",
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["kept_turns"] == 2
    assert body["collapsed_turns"] == 4
    assert body["summarized"] is True
    assert body["used_tokens"] <= body["budget_tokens"]
    assert body["headroom_tokens"] >= 0
    assert any("摘要" in note for note in body["notes"])
    assert all({"role", "content", "tokens"} <= set(m) for m in body["messages"])


def test_context_preview_404s_for_unknown_conversation(client):
    assert client.get("/api/conversations/999/context").status_code == 404
    assert client.get("/api/conversations/999").status_code == 404
