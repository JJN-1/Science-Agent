from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.store.models import BlackboardObject


def _next_version(session: Session, project_id: int, obj_type: str) -> int:
    current = session.scalar(
        select(func.max(BlackboardObject.version)).where(
            BlackboardObject.project_id == project_id,
            BlackboardObject.obj_type == obj_type,
        )
    )
    return (current or 0) + 1


def write(session: Session, *, project_id: int, obj_type: str, payload: dict,
          produced_by: str, evidence: list | None = None) -> BlackboardObject:
    obj = BlackboardObject(
        project_id=project_id,
        obj_type=obj_type,
        version=_next_version(session, project_id, obj_type),
        payload=payload,
        produced_by=produced_by,
        evidence=evidence or [],
    )
    session.add(obj)
    session.flush()
    return obj


def list_for_project(session: Session, project_id: int) -> list[BlackboardObject]:
    return list(
        session.scalars(
            select(BlackboardObject)
            .where(BlackboardObject.project_id == project_id)
            .order_by(BlackboardObject.id)
        )
    )


def latest_by_type(session: Session, project_id: int) -> dict[str, BlackboardObject]:
    latest: dict[str, BlackboardObject] = {}
    for obj in list_for_project(session, project_id):
        latest[obj.obj_type] = obj
    return latest
