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


class RunStageResponse(BaseModel):
    run_id: int
    status: str
