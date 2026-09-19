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
