from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.store.models import TOOL_CALL_STATUSES, ToolCall


def create(
    session: Session,
    *,
    conversation_id: int,
    run_id: int | None,
    tool_name: str,
    args: dict,
    permission: str,
    status: str,
    result: dict | None = None,
    error: str = "",
    duration_ms: int = 0,
    approval_id: int | None = None,
) -> ToolCall:
    """落一条工具调用记录。

    参数是**拆开的原始值**而不是内核对象：store 层不依赖内核层，
    与 ``messages_dao`` 只收算好的 ``tokens``、``task_plans_dao`` 只收 ``to_steps_payload()``
    是同一条分层约定 —— 让 store 认识 ``ToolResult``，store 就得跟着内核一起改。

    ``status`` 必须是三态之一：写成一个拼错的值，事后按状态过滤就会**静默漏掉**记录，
    而这恰恰是审计表最不能出的问题。
    """
    if status not in TOOL_CALL_STATUSES:
        raise ValueError(
            f"未知的工具调用状态：{status}（可选 {'、'.join(TOOL_CALL_STATUSES)}）"
        )
    row = ToolCall(
        conversation_id=conversation_id,
        run_id=run_id,
        tool_name=tool_name,
        args=dict(args or {}),
        permission=permission,
        status=status,
        result=result,
        error=error,
        duration_ms=duration_ms,
        approval_id=approval_id,
    )
    session.add(row)
    session.flush()
    return row


def get(session: Session, tool_call_id: int) -> ToolCall | None:
    return session.get(ToolCall, tool_call_id)


def list_for_conversation(
    session: Session, conversation_id: int, *, limit: int = 200,
) -> list[ToolCall]:
    """按时间正序取（与 ``messages_dao`` 一致：读出来就是发生顺序）。

    正序而不是倒序是刻意的：G2 第 7 条要比对「两次运行的 tool_calls 序列」，
    逐项比较必须从第一次开始 —— 倒序的话每一次比对都要先记得反转。
    """
    return list(
        session.scalars(
            select(ToolCall)
            .where(ToolCall.conversation_id == conversation_id)
            .order_by(ToolCall.id)
            .limit(limit)
        ).all()
    )


def list_for_run(session: Session, run_id: int) -> list[ToolCall]:
    return list(
        session.scalars(
            select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.id)
        ).all()
    )


def count_for_conversation(session: Session, conversation_id: int) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(ToolCall)
            .where(ToolCall.conversation_id == conversation_id)
        ) or 0
    )
