from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.store.models import StageCheckpoint


def save_checkpoint(session: Session, *, project_id: int, stage_id: str,
                    status: str, snapshot: dict) -> StageCheckpoint:
    checkpoint = StageCheckpoint(
        project_id=project_id, stage_id=stage_id, status=status, snapshot=snapshot
    )
    session.add(checkpoint)
    session.flush()
    return checkpoint


def list_for_project(session: Session, project_id: int) -> list[StageCheckpoint]:
    return list(
        session.scalars(
            select(StageCheckpoint)
            .where(StageCheckpoint.project_id == project_id)
            .order_by(StageCheckpoint.id.desc())
        )
    )
