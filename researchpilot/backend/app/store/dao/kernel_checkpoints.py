"""内核检查点的读写（US-407）。

只做搬运，不在这里做判定：**「这一条能不能续跑、从哪一步续」是内核的事**
（``app/agent_kernel/checkpoints.py`` 用纯函数回答），DAO 只负责按 ``conversation_id``
取最近一条/全部。把判定写在 DAO 里会让内核单测必须连数据库 —— 而「能不能续跑」
恰恰是最需要逐种状态断言、又最不该依赖库的东西。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.store.models import KernelCheckpoint


def create(
    session: Session,
    *,
    conversation_id: int,
    plan_id: int | None,
    step_index: int,
    status: str,
    snapshot: dict,
) -> KernelCheckpoint:
    row = KernelCheckpoint(
        conversation_id=conversation_id,
        plan_id=plan_id,
        step_index=step_index,
        status=status,
        snapshot=dict(snapshot),
    )
    session.add(row)
    session.flush()
    return row


def latest_for_conversation(
    session: Session, conversation_id: int,
) -> KernelCheckpoint | None:
    """最近一条检查点。

    按 ``id`` 倒序而不是按 ``created_at``：同一秒内可能落好几条（一次挂起会连着写
    「步骤收口」与「暂停」两条），而同一秒内的时间戳排不出先后。``id`` 是自增的，
    它记的就是写入顺序 —— 顺序在这里是有意义的（谁覆盖谁）。
    """
    return session.scalar(
        select(KernelCheckpoint)
        .where(KernelCheckpoint.conversation_id == conversation_id)
        .order_by(KernelCheckpoint.id.desc())
        .limit(1)
    )


def list_for_conversation(
    session: Session, conversation_id: int, limit: int = 50,
) -> list[KernelCheckpoint]:
    """新的在前。与 ``latest_for_conversation`` 同一条排序约定。"""
    return list(
        session.scalars(
            select(KernelCheckpoint)
            .where(KernelCheckpoint.conversation_id == conversation_id)
            .order_by(KernelCheckpoint.id.desc())
            .limit(limit)
        )
    )


def count_for_conversation(session: Session, conversation_id: int) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(KernelCheckpoint)
            .where(KernelCheckpoint.conversation_id == conversation_id)
        )
        or 0
    )
