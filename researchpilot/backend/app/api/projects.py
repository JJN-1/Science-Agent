from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.schemas import ProjectCreate, ProjectOut, ProjectUpdate
from app.store.dao import projects as projects_dao

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, session: Session = Depends(get_session)) -> ProjectOut:
    project = projects_dao.create(session, **payload.model_dump())
    session.flush()
    return ProjectOut.model_validate(project)


@router.get("", response_model=list[ProjectOut])
def list_projects(session: Session = Depends(get_session)) -> list[ProjectOut]:
    return [ProjectOut.model_validate(p) for p in projects_dao.list_all(session)]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, session: Session = Depends(get_session)) -> ProjectOut:
    project = projects_dao.get(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ProjectOut.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int, payload: ProjectUpdate, session: Session = Depends(get_session)
) -> ProjectOut:
    project = projects_dao.update(session, project_id, **payload.model_dump(exclude_none=True))
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ProjectOut.model_validate(project)
