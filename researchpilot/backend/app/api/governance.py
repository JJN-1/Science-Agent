from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.store.dao import approvals as approvals_dao
from app.store.dao import decisions as decisions_dao
from app.store.dao import grants as grants_dao
from app.store.dao import usage as usage_dao

router = APIRouter(tags=["governance"])

# 批准后签发的预算豁免默认有效期（小时）。豁免会过期，避免一次批准永久放开治理。
GRANT_TTL_HOURS = 24


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
def usage_summary(project_id: int | None = None, dim: str = "agent",
                  session: Session = Depends(get_session)) -> dict:
    """成本与调用次数归因（US-203）。

    ``project_id`` 可省略 —— 不传即全库汇总。设置页的「模型供应商统计」问的是
    「我这个后端到底被调过几次」，那是全局问题；按项目切片答不了。
    合计一并返回，免得每个调用方各自求和（并各自求错）。
    """
    try:
        rows = usage_dao.summary_by(session, project_id, dim)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"未知归因维度: {dim}") from None
    return {
        "dim": dim,
        "project_id": project_id,
        "rows": rows,
        "total_cost": sum(r["cost"] for r in rows),
        "total_calls": sum(r["calls"] for r in rows),
        "total_failed": sum(r["failed"] for r in rows),
    }


class ApprovalDecision(BaseModel):
    note: str = ""
    # 可覆盖默认建议额度；留空则采用审批单里的 suggested_grant
    grant_amount: float | None = None


@router.get("/api/projects/{project_id}/budget-grants")
def project_budget_grants(project_id: int,
                          session: Session = Depends(get_session)) -> list[dict]:
    """预算豁免审计：谁批的（approval_id）、批了多少、有效期到什么时候。"""
    return [_grant_out(g) for g in grants_dao.list_for_project(session, project_id)]


@router.get("/api/approvals")
def list_approvals(project_id: int | None = None, status: str = "pending",
                   session: Session = Depends(get_session)) -> list[dict]:
    return [_approval_out(a) for a in approvals_dao.list_by_status(session, project_id, status)]


@router.post("/api/approvals/{approval_id}/approve")
def approve(approval_id: int, request: Request, body: ApprovalDecision,
            session: Session = Depends(get_session)) -> dict:
    return _decide_and_resume(approval_id, request, session, "approved", body)


@router.post("/api/approvals/{approval_id}/reject")
def reject(approval_id: int, request: Request, body: ApprovalDecision,
           session: Session = Depends(get_session)) -> dict:
    return _decide_and_resume(approval_id, request, session, "rejected", body)


def _grant_out(g) -> dict:  # noqa: ANN001
    return {
        "id": g.id,
        "project_id": g.project_id,
        "scope": g.scope,
        "agent_id": g.agent_id,
        "amount": g.amount,
        "approval_id": g.approval_id,
        "expires_at": g.expires_at.isoformat() if g.expires_at else None,
        "created_at": g.created_at.isoformat(),
    }


def _approval_out(a) -> dict:  # noqa: ANN001
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
                       status: str, body: ApprovalDecision) -> dict:
    """审批流转。批准预算熔断时先签发豁免，再重跑该阶段（FIX-02）。

    两处顺序都必须守住：
    1. 豁免必须**先落库**，重跑时 BudgetManager 才能算出提高后的有效限额，
       否则重跑会立刻再次熔断——这正是修复前的死循环。
    2. 审批结果与豁免必须先 commit，再尝试重跑。若重跑因别的原因失败（模型不可用等），
       绝不能让人的批准动作跟着一起被回滚——那会让审批单诡异地「回到待审」。
    """
    approval = approvals_dao.decide(session, approval_id, status)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")

    result: dict = {"approval_id": approval_id, "status": status}
    if status != "approved" or approval.kind != "budget":
        return result

    detail = approval.detail or {}
    project_id = approval.project_id
    scope = detail.get("source") or "project_total"
    gateway = getattr(request.app.state, "gateway", None)
    policy = getattr(getattr(gateway, "budget", None), "grant_policy", {})
    amount = body.grant_amount
    if amount is None:
        amount = float(detail.get("suggested_grant") or policy.get(scope, 0.0))
    amount = float(amount)

    expires_at = datetime.now(UTC) + timedelta(hours=GRANT_TTL_HOURS)
    grant = grants_dao.create(
        session, project_id=project_id, scope=scope, amount=amount,
        agent_id=detail.get("agent_id"), approval_id=approval.id,
        expires_at=expires_at,
    )
    decisions_dao.add(
        session, project_id=project_id, run_id=approval.run_id,
        stage_id=detail.get("stage_id") or "",
        agent_id=detail.get("agent_id") or "",
        decision=f"批准追加 {scope} 额度 {amount:.2f}（{GRANT_TTL_HOURS}h 内有效）",
        reason=body.note or "人工审批通过",
        decided_by="user",
    )
    grant_payload = _grant_out(grant)
    session.commit()  # 审批与豁免就此生效，不受后续重跑成败影响
    result["grant"] = grant_payload

    stage_id = detail.get("stage_id")
    if not stage_id:
        return result
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        return result
    try:
        result["new_run_id"] = orchestrator.run_stage(session, project_id, stage_id)
    except Exception as exc:
        # 重跑失败不该吞掉：commit 保留失败现场的轨迹，并把原因回给调用方
        try:
            session.commit()
        except Exception:
            session.rollback()
        result["new_run_id"] = None
        result["resume_error"] = str(exc)
    return result
