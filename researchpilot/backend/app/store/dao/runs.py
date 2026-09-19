from __future__ import annotations

from datetime import UTC

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.store.models import AgentRun, AgentStep


def create_run(session: Session, *, project_id: int, stage_id: str, agent_id: str) -> AgentRun:
    run = AgentRun(project_id=project_id, stage_id=stage_id, agent_id=agent_id)
    session.add(run)
    session.flush()
    return run


def add_step(session: Session, *, run_id: int, kind: str, content: dict) -> AgentStep:
    seq = (session.scalar(select(func.max(AgentStep.seq)).where(AgentStep.run_id == run_id)) or 0) + 1
    step = AgentStep(run_id=run_id, seq=seq, kind=kind, content=content)
    session.add(step)
    session.flush()
    run = session.get(AgentRun, run_id)
    if run is not None:
        run.steps = seq
    return step


def finish_run(session: Session, *, run_id: int, status: str, error: str | None = None) -> AgentRun | None:
    from datetime import datetime

    run = session.get(AgentRun, run_id)
    if run is None:
        return None
    run.status = status
    run.error = error
    run.finished_at = datetime.now(UTC)
    session.flush()
    return run


def get_run(session: Session, run_id: int) -> AgentRun | None:
    return session.get(AgentRun, run_id)


def list_steps(session: Session, run_id: int) -> list[AgentStep]:
    return list(
        session.scalars(
            select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.seq)
        )
    )


def list_for_project(session: Session, project_id: int) -> list[AgentRun]:
    return list(
        session.scalars(
            select(AgentRun).where(AgentRun.project_id == project_id).order_by(AgentRun.id.desc())
        )
    )
