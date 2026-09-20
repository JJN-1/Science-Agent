from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.store.models import Conversation


def create(session: Session, *, project_id: int, title: str = "") -> Conversation:
    conversation = Conversation(project_id=project_id, title=title, status="active")
    session.add(conversation)
    session.flush()
    return conversation


def get(session: Session, conversation_id: int) -> Conversation | None:
    return session.get(Conversation, conversation_id)


def list_for_project(
    session: Session,
    project_id: int,
    *,
    limit: int = 50,
    include_archived: bool = False,
) -> list[Conversation]:
    """项目的会话列表，新的在前。

    缺省只列 ``active``：归档的作用就是「从默认视野里消失」，
    但**不删数据** —— 归档会话仍可被 id 直接取到。
    """
    stmt = select(Conversation).where(Conversation.project_id == project_id)
    if not include_archived:
        stmt = stmt.where(Conversation.status == "active")
    return list(session.scalars(stmt.order_by(Conversation.id.desc()).limit(limit)).all())


def rename(session: Session, conversation_id: int, title: str) -> Conversation | None:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        return None
    conversation.title = title
    session.flush()
    return conversation


def archive(session: Session, conversation_id: int) -> Conversation | None:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        return None
    conversation.status = "archived"
    session.flush()
    return conversation


def touch(session: Session, conversation_id: int) -> None:
    """刷新 ``updated_at``。

    必须显式调用：ORM 的 ``onupdate`` 只在**该行被 UPDATE 时**触发，而追加消息
    动的是 ``messages`` 表 —— 会话行本身没变，``updated_at`` 会一直停在创建时刻，
    会话列表按它排序就是错的。
    """
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        return
    conversation.updated_at = datetime.now(UTC)
    session.flush()
