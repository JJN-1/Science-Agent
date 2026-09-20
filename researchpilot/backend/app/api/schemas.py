from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    domain: str = "cs-ai"
    goal: str = ""


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    domain: str | None = None
    goal: str | None = None
    status: str | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    domain: str
    goal: str
    status: str
    created_at: datetime
    updated_at: datetime


class StageOut(BaseModel):
    stage_id: str
    agent_id: str
    name: str
    description: str
    implemented: bool
    planned_sprint: int | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    stage_id: str
    agent_id: str
    status: str
    steps: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    seq: int
    kind: str
    content: dict
    created_at: datetime


class RunDetailOut(RunOut):
    steps: list[StepOut] = []


class JobAccepted(BaseModel):
    """受理回执（FIX-03）：受理只承诺「已排队」，不等于「已跑完」。"""

    job_id: int
    status: str


class JobOut(BaseModel):
    """作业快照，供轮询回退与排查使用。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    kind: str
    stage_id: str | None
    status: str
    run_id: int | None
    error: str | None
    params: dict
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    seq: int
    type: str
    payload: dict
    created_at: datetime


class PipelineRunRequest(BaseModel):
    """可选：只跑指定的阶段子集，缺省按 STAGE_ORDER 跑全部已注册阶段。"""

    stage_ids: list[str] | None = None


class ConversationCreate(BaseModel):
    project_id: int
    title: str = Field(default="", max_length=255)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    status: str | None = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    status: str
    created_at: datetime
    updated_at: datetime


class MessageCreate(BaseModel):
    """追加一条消息。``role`` 默认 ``user``：内核回填 assistant / tool 走内部路径，
    不通过这个入口 —— 否则客户端可以伪造「助手说过什么」。"""

    content: str = Field(min_length=1)
    role: str = "user"


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    role: str
    content: str
    tool_call_id: str | None
    tokens: int
    created_at: datetime


class MessageAccepted(BaseModel):
    """追加消息的回执（D14）。

    ``job_id`` 在 Sprint 4 第 1 步**恒为 null** —— 内核循环尚未接上时返回一个假 id
    比返回 null 更糟：前端会去订阅一个不存在的作业。第 5 步接上内核后，
    这里变成 ``kind=chat`` 作业的真实 id，而契约形状不变。
    """

    message: MessageOut
    job_id: int | None = None


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut] = []
    total_tokens: int = 0


class ContextPreviewOut(BaseModel):
    """上下文装配的可视化预览（US-402）。

    「裁剪了什么、为什么裁」如果只写在日志里，就没有人会发现它裁错了。
    这个接口把装配结果摆出来，同时也是「压缩存活」这类探针的落点。
    """

    budget_tokens: int
    used_tokens: int
    headroom_tokens: int
    kept_turns: int
    collapsed_turns: int
    dropped_turns: int
    summarized: bool
    notes: list[str]
    messages: list[dict]
