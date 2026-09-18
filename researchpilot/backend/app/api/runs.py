from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.schemas import RunDetailOut, RunOut, StepOut
from app.store.dao import runs as runs_dao

router = APIRouter(prefix="/api", tags=["runs"])


@router.get("/projects/{project_id}/runs", response_model=list[RunOut])
def list_runs(project_id: int, session: Session = Depends(get_session)) -> list[RunOut]:
    return [RunOut.model_validate(r) for r in runs_dao.list_for_project(session, project_id)]


@router.get("/runs/{run_id}", response_model=RunDetailOut)
def get_run(run_id: int, session: Session = Depends(get_session)) -> RunDetailOut:
    run = runs_dao.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    data = RunOut.model_validate(run).model_dump()
    data["steps"] = [StepOut.model_validate(s).model_dump() for s in runs_dao.list_steps(session, run_id)]
    return RunDetailOut(**data)
