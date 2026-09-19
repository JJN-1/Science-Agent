from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.store.dao import approvals as approvals_dao
from app.store.dao import decisions as decisions_dao
from app.store.dao import usage as usage_dao

router = APIRouter(tags=["governance"])


@router.get("/api/projects/{project_id}/decisions")
def project_decisions(project_id: int, session: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "id": d.id,
            "run_id": d.run_id,
            "stage_id": d.stage_id,
            "agent_id": d.agent_id,
            "kind": d.kind,
            "decision": d.decision,
            "reason": d.reason,
            "alternatives": d.alternatives,
            "decided_by": d.decided_by,
            "created_at": d.created_at.isoformat(),
        }
        for d in decisions_dao.list_for_project(session, project_id)
    ]


@router.get("/api/usage/summary")
def usage_summary(project_id: int, dim: str = "agent",
                  session: Session = Depends(get_session)) -> dict:
    try:
        rows = usage_dao.summary_by(session, project_id, dim)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"未知归因维度: {dim}") from None
    return {
        "dim": dim,
        "project_id": project_id,
        "rows": rows,
        "total_cost": sum(r["cost"] for r in rows),
    }


class ApprovalDecision(BaseModel):
    note: str = ""


@router.get("/api/approvals")
def list_approvals(project_id: int | None = None, status: str = "pending",
                   session: Session = Depends(get_session)) -> list[dict]:
    return [_approval_out(a) for a in approvals_dao.list_by_status(session, project_id, status)]


@router.post("/api/approvals/{approval_id}/approve")
def approve(approval_id: int, request: Request, body: ApprovalDecision,
            session: Session = Depends(get_session)) -> dict:
    return _decide_and_resume(approval_id, request, session, "approved")


@router.post("/api/approvals/{approval_id}/reject")
def reject(approval_id: int, request: Request, body: ApprovalDecision,
           session: Session = Depends(get_session)) -> dict:
    return _decide_and_resume(approval_id, request, session, "rejected")


def _approval_out(a) -> dict:
    return {
        "id": a.id,
        "project_id": a.project_id,
        "run_id": a.run_id,
        "kind": a.kind,
        "detail": a.detail,
        "status": a.status,
        "created_at": a.created_at.isoformat(),
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
    }


def _decide_and_resume(approval_id: int, request: Request, session: Session,
                       status: str) -> dict:
    approval = approvals_dao.decide(session, approval_id, status)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    result: dict = {"approval_id": approval_id, "status": status}
    # 批准后恢复：重跑该阶段（US-205 演示路径）
    if status == "approved" and approval.kind == "budget":
        stage_id = (approval.detail or {}).get("stage_id")
        orchestrator = request.app.state.orchestrator
        try:
            new_run_id = orchestrator.run_stage(session, approval.project_id, stage_id)
        except KeyError:
            new_run_id = None
        result["new_run_id"] = new_run_id
    return result
