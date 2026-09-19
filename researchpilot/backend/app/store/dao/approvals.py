from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.store.models import Approval


def create(
    session: Session,
    project_id: int,
    kind: str,
    detail: dict,
    run_id: int | None = None,
) -> Approval:
    row = Approval(project_id=project_id, run_id=run_id, kind=kind, detail=detail)
    session.add(row)
    return row


def get(session: Session, approval_id: int) -> Approval | None:
    return session.get(Approval, approval_id)


def list_by_status(session: Session, project_id: int | None = None,
                   status: str = "pending") -> list[Approval]:
    stmt = select(Approval).where(Approval.status == status).order_by(Approval.id)
    if project_id is not None:
        stmt = stmt.where(Approval.project_id == project_id)
    return list(session.scalars(stmt).all())


def decide(session: Session, approval_id: int, status: str) -> Approval | None:
    row = get(session, approval_id)
    if row is None:
        return None
    row.status = status
    row.decided_at = datetime.now(UTC)  # 补记审批时间，此前一直为 NULL
    session.flush()
    return row
