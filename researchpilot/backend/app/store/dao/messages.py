from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.store.dao import conversations as conversations_dao
from app.store.models import Message


def create(
    session: Session,
    *,
    conversation_id: int,
    role: str,
    content: str,
    tool_call_id: str | None = None,
    tool_calls: list[dict] | None = None,
    tokens: int,
) -> Message:
    """追加一条消息。

    ``tokens`` 是**必填**的：留成可选就等于允许写入 ``0``，
    而 ``0`` 会让下一次上下文裁剪以为这条消息不占预算 —— 静默的超窗比报错更难查。
    估算函数在 ``agent_kernel.tokens``，由调用方（API / 内核）算好传进来，
    这样 store 层不必反向依赖内核层。

    ``tool_calls`` 只在 ``role="assistant"`` 且该轮调了工具时出现（US-405）。
    它必须与随后的 ``tool`` 结果消息**成对落库**：协议要求 tool 结果能对应上带
    ``tool_calls`` 的 assistant 消息，而历史是跨请求复用的 —— 只落一半，
    第二句话就会被上游 400 拒收。
    """
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        tool_call_id=tool_call_id,
        tool_calls=list(tool_calls) if tool_calls else None,
        tokens=tokens,
    )
    session.add(message)
    session.flush()
    # 会话列表按 updated_at 排序，追加消息必须把会话行也推一下
    conversations_dao.touch(session, conversation_id)
    return message


def get(session: Session, message_id: int) -> Message | None:
    return session.get(Message, message_id)


def list_for_conversation(
    session: Session,
    conversation_id: int,
    *,
    limit: int | None = None,
) -> list[Message]:
    """按时间正序取消息（装配上下文用的顺序）。

    ``limit`` 截的是**最近的 N 条**而不是最早的 N 条：需要限流时，
    有价值的永远是离现在最近的那段对话。
    """
    stmt = select(Message).where(Message.conversation_id == conversation_id)
    if limit is None:
        return list(session.scalars(stmt.order_by(Message.id)).all())
    newest = list(session.scalars(stmt.order_by(Message.id.desc()).limit(limit)).all())
    return list(reversed(newest))


def count_for_conversation(session: Session, conversation_id: int) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(Message)
            .where(Message.conversation_id == conversation_id)
        ) or 0
    )


def total_tokens(session: Session, conversation_id: int) -> int:
    """会话内所有消息的估算 token 合计（界面上的「这次对话用了多少」）。"""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(Message.tokens), 0))
            .where(Message.conversation_id == conversation_id)
        ) or 0
    )
