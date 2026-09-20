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


def test_append_message_returns_null_job_id_and_estimated_tokens(client):
    """D14：内核未就位时 job_id 必须是 null，且响应形状已定型，第 5 步只换值。"""
    project = client.post("/api/projects", json={"title": "P"}).json()
    conversation = client.post(
        "/api/conversations", json={"project_id": project["id"]},
    ).json()

    resp = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "研究一下稀疏注意力"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["job_id"] is None
    assert body["message"]["role"] == "user"
    assert body["message"]["tokens"] > 0


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
