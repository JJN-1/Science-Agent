from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.store.models import Project


def create(session: Session, *, title: str, domain: str = "cs-ai", goal: str = "") -> Project:
    project = Project(title=title, domain=domain, goal=goal)
    session.add(project)
    session.flush()
    return project


def get(session: Session, project_id: int) -> Project | None:
    return session.get(Project, project_id)


def list_all(session: Session) -> list[Project]:
    return list(session.scalars(select(Project).order_by(Project.id.desc())))


def update(session: Session, project_id: int, *, title: str | None = None,
           domain: str | None = None, goal: str | None = None,
           status: str | None = None) -> Project | None:
    project = session.get(Project, project_id)
    if project is None:
        return None
    for field, value in (("title", title), ("domain", domain), ("goal", goal), ("status", status)):
        if value is not None:
            setattr(project, field, value)
    session.flush()
    return project
