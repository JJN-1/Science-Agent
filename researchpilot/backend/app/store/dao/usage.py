from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.store.models import LlmUsage

# 记账状态：失败行 cost=0，只进调用次数与 failed 计数，不进花费合计。
STATUS_OK = "ok"
STATUS_FAILED = "failed"


def record(
    session: Session,
    stage_id: str,
    agent_id: str,
    provider: str,
    model: str,
    tier: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: float = 0.0,
    latency_ms: int = 0,
    cached: bool = False,
    degraded: list | None = None,
    project_id: int | None = None,
    run_id: int | None = None,
    status: str = STATUS_OK,
    error: str | None = None,
    attempts: int = 1,
) -> LlmUsage:
    row = LlmUsage(
        project_id=project_id,
        run_id=run_id,
        stage_id=stage_id,
        agent_id=agent_id,
        provider=provider,
        model=model,
        tier=tier,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        latency_ms=latency_ms,
        cached=cached,
        degraded=degraded or [],
        status=status,
        error=error,
        attempts=attempts,
    )
    session.add(row)
    return row


def project_spend(session: Session, project_id: int) -> float:
    return float(
        session.scalar(
            select(func.coalesce(func.sum(LlmUsage.cost), 0.0)).where(
                LlmUsage.project_id == project_id
            )
        )
    )


def project_spend_today(session: Session, project_id: int) -> float:
    day_start = func.date(LlmUsage.created_at, "localtime")
    today = func.date("now", "localtime")
    return float(
        session.scalar(
            select(func.coalesce(func.sum(LlmUsage.cost), 0.0)).where(
                LlmUsage.project_id == project_id,
                day_start == today,
            )
        )
    )


def run_cost(session: Session, run_id: int) -> float:
    return float(
        session.scalar(
            select(func.coalesce(func.sum(LlmUsage.cost), 0.0)).where(
                LlmUsage.run_id == run_id
            )
        )
    )


def summary_by(session: Session, project_id: int, dim: str) -> list[dict]:
    """三维归因：dim ∈ {stage, agent, provider}。

    ``calls`` 含失败调用，``failed`` 单列失败次数 —— 只统计成功的话，
    「这个后端到底被调过几次」这个问题永远答不对。
    """
    columns = {"stage": LlmUsage.stage_id, "agent": LlmUsage.agent_id,
               "provider": LlmUsage.provider}
    key = columns.get(dim)
    if key is None:
        raise ValueError(f"unknown usage dim: {dim}")
    failed = func.coalesce(
        func.sum(case((LlmUsage.status != STATUS_OK, 1), else_=0)), 0
    )
    rows = session.execute(
        select(
            key,
            func.count(LlmUsage.id),
            func.coalesce(func.sum(LlmUsage.prompt_tokens), 0),
            func.coalesce(func.sum(LlmUsage.completion_tokens), 0),
            func.coalesce(func.sum(LlmUsage.cost), 0.0),
            failed,
        )
        .where(LlmUsage.project_id == project_id)
        .group_by(key)
        .order_by(func.sum(LlmUsage.cost).desc())
    ).all()
    return [
        {
            "key": row[0],
            "calls": row[1],
            "failed": int(row[5]),
            "prompt_tokens": int(row[2]),
            "completion_tokens": int(row[3]),
            "cost": float(row[4]),
        }
        for row in rows
    ]
