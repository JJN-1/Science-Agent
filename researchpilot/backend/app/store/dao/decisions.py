from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.store.models import Decision


def add(
    session: Session,
    project_id: int,
    stage_id: str,
    agent_id: str,
    decision: str,
    reason: str = "",
    alternatives: list | None = None,
    decided_by: str = "agent",
    kind: str = "decision",
    run_id: int | None = None,
) -> Decision:
    row = Decision(
        project_id=project_id,
        run_id=run_id,
        stage_id=stage_id,
        agent_id=agent_id,
        kind=kind,
        decision=decision,
        reason=reason,
        alternatives=alternatives or [],
        decided_by=decided_by,
    )
    session.add(row)
    return row


def list_for_project(session: Session, project_id: int) -> list[Decision]:
    return list(
        session.scalars(
            select(Decision)
            .where(Decision.project_id == project_id)
            .order_by(Decision.id)
        ).all()
    )
