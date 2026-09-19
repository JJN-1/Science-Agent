from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.store.models import BudgetGrant


def create(
    session: Session,
    *,
    project_id: int,
    scope: str,
    amount: float,
    agent_id: str | None = None,
    approval_id: int | None = None,
    expires_at: datetime | None = None,
) -> BudgetGrant:
    grant = BudgetGrant(
        project_id=project_id,
        scope=scope,
        agent_id=agent_id,
        amount=amount,
        approval_id=approval_id,
        expires_at=expires_at,
    )
    session.add(grant)
    session.flush()
    return grant


def _is_active(grant: BudgetGrant, now: datetime) -> bool:
    if grant.expires_at is None:
        return True
    expires_at = grant.expires_at
    if expires_at.tzinfo is None:  # SQLite 存的是 naive UTC
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > now


def granted(session: Session, project_id: int, scope: str,
            agent_id: str | None = None) -> float:
    """项目在指定 scope 下「仍未过期」的豁免额度合计（FIX-02 有效限额的加项）。"""
    stmt = select(BudgetGrant).where(
        BudgetGrant.project_id == project_id, BudgetGrant.scope == scope
    )
    if agent_id is not None:
        stmt = stmt.where(BudgetGrant.agent_id == agent_id)
    now = datetime.now(UTC)
    return float(sum(g.amount for g in session.scalars(stmt).all() if _is_active(g, now)))


def list_for_project(session: Session, project_id: int) -> list[BudgetGrant]:
    return list(
        session.scalars(
            select(BudgetGrant)
            .where(BudgetGrant.project_id == project_id)
            .order_by(BudgetGrant.id.desc())
        ).all()
    )
