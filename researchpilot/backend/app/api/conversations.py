from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agent_kernel import context as kernel_context
from app.agent_kernel.tokens import estimate_message_tokens
from app.api.deps import get_session
from app.api.schemas import (
    ContextPreviewOut,
    ConversationCreate,
    ConversationDetailOut,
    ConversationOut,
    ConversationUpdate,
    MessageAccepted,
    MessageCreate,
    MessageOut,
)
from app.store.dao import conversations as conversations_dao
from app.store.dao import messages as messages_dao
from app.store.dao import projects as projects_dao

router = APIRouter(tags=["conversations"])

#: 详情接口默认带回的消息条数
DETAIL_MESSAGE_LIMIT = 200


@router.post("/api/conversations", response_model=ConversationOut, status_code=201)
def create_conversation(payload: ConversationCreate, session: Session = Depends(get_session)):
    if projects_dao.get(session, payload.project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return conversations_dao.create(
        session, project_id=payload.project_id, title=payload.title,
    )


@router.get("/api/projects/{project_id}/conversations", response_model=list[ConversationOut])
def list_project_conversations(
    project_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    include_archived: bool = False,
    session: Session = Depends(get_session),
):
    if projects_dao.get(session, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return conversations_dao.list_for_project(
        session, project_id, limit=limit, include_archived=include_archived,
    )


@router.get("/api/conversations/{conversation_id}", response_model=ConversationDetailOut)
def get_conversation(
    conversation_id: int,
    limit: int = Query(default=DETAIL_MESSAGE_LIMIT, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    conversation = conversations_dao.get(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    rows = messages_dao.list_for_conversation(session, conversation_id, limit=limit)
    return ConversationDetailOut(
        **ConversationOut.model_validate(conversation).model_dump(),
        messages=[MessageOut.model_validate(row) for row in rows],
        total_tokens=messages_dao.total_tokens(session, conversation_id),
    )


@router.patch("/api/conversations/{conversation_id}", response_model=ConversationOut)
def update_conversation(
    conversation_id: int,
    payload: ConversationUpdate,
    session: Session = Depends(get_session),
):
    conversation = conversations_dao.get(session, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    if payload.title is not None:
        conversation = conversations_dao.rename(session, conversation_id, payload.title)
    if payload.status is not None:
        if payload.status == "archived":
            conversation = conversations_dao.archive(session, conversation_id)
        elif payload.status == "active":
            conversation.status = "active"
            session.flush()
        else:
            raise HTTPException(status_code=400, detail=f"未知状态：{payload.status}")
    return conversation


@router.post(
    "/api/conversations/{conversation_id}/messages",
    response_model=MessageAccepted,
    status_code=201,
)
def append_message(
    conversation_id: int,
    payload: MessageCreate,
    session: Session = Depends(get_session),
):
    """追加一条用户消息（D14：``job_id`` 暂为 null）。

    ``role`` 只接受 ``user``：assistant / tool 消息由内核在进程内直接写库。
    开放角色字段等于允许客户端伪造「助手说过什么」，而那会污染审计轨迹 ——
    轨迹的价值恰恰在于它只可能由系统自己产生。
    """
    if conversations_dao.get(session, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if payload.role != "user":
        raise HTTPException(
            status_code=400,
            detail="该接口只接受 role=user；assistant/tool 消息由内核在进程内写入",
        )

    row = messages_dao.create(
        session,
        conversation_id=conversation_id,
        role="user",
        content=payload.content,
        tokens=estimate_message_tokens("user", payload.content),
    )
    # 第一条消息顺手把会话标题定下来，省掉用户「建完会话还要再起个名」的一步
    conversation = conversations_dao.get(session, conversation_id)
    if conversation is not None and not conversation.title:
        conversations_dao.rename(session, conversation_id, payload.content.strip()[:40])

    return MessageAccepted(message=MessageOut.model_validate(row), job_id=None)


@router.get(
    "/api/conversations/{conversation_id}/context",
    response_model=ContextPreviewOut,
)
def preview_context(
    conversation_id: int,
    budget_tokens: int = Query(
        default=kernel_context.DEFAULT_BUDGET_TOKENS, ge=256, le=1_000_000,
    ),
    recent_turns: int = Query(
        default=kernel_context.DEFAULT_RECENT_TURNS, ge=0, le=100,
    ),
    session: Session = Depends(get_session),
):
    """预览一次装配会往模型送什么（US-402）。

    裁剪是**静默**的：没有这个接口，用户只会看到模型「忘了刚才说过的话」，
    而没有任何办法确认是不是裁剪干的。这里把账摊开。
    """
    if conversations_dao.get(session, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    rows = messages_dao.list_for_conversation(session, conversation_id)
    history = [kernel_context.ContextMessage.from_row(row) for row in rows]
    result = kernel_context.assemble(
        history, budget_tokens=budget_tokens, recent_turns=recent_turns,
    )
    return ContextPreviewOut(
        budget_tokens=result.budget_tokens,
        used_tokens=result.used_tokens,
        headroom_tokens=result.headroom_tokens,
        kept_turns=result.kept_turns,
        collapsed_turns=result.collapsed_turns,
        dropped_turns=result.dropped_turns,
        summarized=result.summarized,
        notes=list(result.notes),
        messages=[
            {
                "role": m.role,
                "content": m.content,
                "tool_call_id": m.tool_call_id,
                "tokens": m.tokens,
            }
            for m in result.messages
        ],
    )
