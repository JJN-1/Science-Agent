from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent_kernel.permissions import APPROVAL_KIND_DANGEROUS
from app.api.deps import get_session
from app.store.dao import app_config as app_config_dao
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
    """审批流转。三种审批各行其道，但都守住同一条：**先落库，再恢复**。

    两处顺序都必须守住：

    1. 豁免/审批记忆必须**先落库**，恢复时才能算出提高后的有效限额、或让闸门认出
       「这条已经批过了」—— 否则恢复会立刻再次熔断、或立刻再次要求批准，
       形成「批准 → 又暂停」的死循环。
    2. 审批结果与豁免必须先 commit，再尝试恢复。若恢复因别的原因失败（模型不可用等），
       绝不能让人的批准动作跟着一起被回滚 —— 那会让审批单诡异地「回到待审」。
    """
    approval = approvals_dao.decide(session, approval_id, status)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")

    result: dict = {"approval_id": approval_id, "status": status}
    if status != "approved":
        # 拒绝就是拒绝：不签任何记忆、不恢复。写下一份「记忆」再拒绝，
        # 等于拒绝按钮顺带把「已批准」也做了。
        return result

    detail = approval.detail or {}
    if approval.kind == APPROVAL_KIND_DANGEROUS:
        return _approve_tool_calls(approval, request, session, body, result)
    if approval.kind != "budget":
        return result

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

    if detail.get("conversation_id"):
        # ⚠️ **会话路径的预算暂停不能走 ``run_stage``**。它的 ``stage_id`` 是 ``chat``，
        # 而 ``chat`` 不是一个注册阶段 —— 走老路会抛「未知阶段」，被下面的兜底接住，
        # 用户看到的是「批准成功了，但恢复失败」。这是 US-406 之前就存在的窟窿，
        # 在这里一并收掉：会话的恢复本来就该回到 ``kind=chat`` 那条通道上。
        _submit_chat_resume(request, session, approval, detail, calls=[])
        session.commit()
        return result

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


def _approve_tool_calls(approval, request: Request, session: Session,
                        body: ApprovalDecision, result: dict) -> dict:
    """批准一次危险/首次执行动作（US-406）。

    批准之后要做两件事，顺序不能反：

    1. **先签记忆**（只有 ``execute`` 档有 ``grant_key``；``dangerous`` 恒为 ``None``）。
       ``dangerous`` 刻意不留记忆 —— 一次批准换永久放行，等于把审批疲劳变成绕过审批。
    2. **再回到作业通道恢复**。恢复不在这个 HTTP 请求里同步跑完：那条命令可能是
       几分钟的训练任务，同步跑会撞 HTTP 超时，而作业通道早就有 SSE 续传、
       取消、僵尸自愈这一整套。
    """
    detail = approval.detail or {}
    round_calls = detail.get("round") or detail.get("pending") or []
    if not detail.get("conversation_id"):
        # 没有会话就无从恢复 —— 内核循环的一切恢复入口都以会话为坐标。
        # 与其猜一个，不如如实说「这张单子没法自动恢复」，让人去看现场。
        result["resume_error"] = "审批单缺少 conversation_id，无法自动恢复执行"
        session.commit()
        return result

    granted: list[str] = []
    stamp = datetime.now(UTC).isoformat()
    for call in round_calls:
        key = call.get("grant_key")
        if not call.get("needs_approval") or not key:
            continue
        app_config_dao.put(session, key, {
            "tool": call.get("tool"),
            "permission": call.get("permission"),
            "granted_at": stamp,
            "approval_id": approval.id,
            "conversation_id": detail.get("conversation_id"),
        })
        granted.append(key)

    tools = "、".join(dict.fromkeys(
        str(call.get("tool")) for call in round_calls if call.get("needs_approval")
    ))
    decisions_dao.add(
        session, project_id=approval.project_id, run_id=approval.run_id,
        stage_id=detail.get("stage_id") or "",
        agent_id=detail.get("agent_id") or "",
        decision=f"批准执行需要授权的工具调用：{tools or '（无）'}",
        reason=body.note or "人工审批通过",
        decided_by="user",
    )
    if granted:
        result["granted"] = granted
    session.commit()

    job = _submit_chat_resume(request, session, approval, detail, calls=round_calls)
    if job is not None:
        result["job_id"] = job.id
    return result


def _submit_chat_resume(request: Request, session: Session, approval,
                        detail: dict, *, calls: list[dict]):
    """把「继续这个会话」派成一个 ``kind=chat`` 作业。

    ``calls`` 非空表示先重放这一轮被批准的工具调用，再继续循环；
    为空则只是「接着往下跑」（预算熔断恢复走这条）。

    内核没装配时返回 ``None`` 而**不是**抛错：窄测试里只跑会话持久化，
    此时如实告诉调用方「没有派活」，比造一个假的 job_id 好。
    """
    job_runner = getattr(request.app.state, "job_runner", None)
    if job_runner is None:
        return None
    job = job_runner.submit(
        session, project_id=approval.project_id, kind="chat",
        params={
            "conversation_id": int(detail["conversation_id"]),
            "agent_id": detail.get("agent_id"),
            "approval_id": approval.id,
            "approve_calls": list(calls),
        },
    )
    session.commit()
    return job
