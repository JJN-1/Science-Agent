from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agent_kernel import planner
from app.agent_kernel.errors import PlanError
from app.api.deps import get_session
from app.api.schemas import TaskPlanCreate, TaskPlanOut, TaskPlanUpdate
from app.store.dao import conversations as conversations_dao
from app.store.dao import task_plans as task_plans_dao

router = APIRouter(tags=["task-plans"])

#: 计划内容非法、模式冲突 → 400；违反编辑/状态规则 → 409（请求本身不合法，与内容无关）
_PLAN_CONFLICT_CODE = "AGENT-PLAN-003"


def _as_http(exc: PlanError) -> HTTPException:
    """把内核错误码翻成 HTTP 语义。

    不统一成 400：内容非法与「这次操作本身不该发生」需要不同的前端处置 ——
    前者改请求体重试，后者要刷新界面看最新状态。
    """
    status = 409 if exc.code == _PLAN_CONFLICT_CODE else 400
    return HTTPException(status_code=status, detail=str(exc))


def _require_conversation(session: Session, conversation_id: int) -> None:
    if conversations_dao.get(session, conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")


@router.post(
    "/api/conversations/{conversation_id}/task-plans",
    response_model=TaskPlanOut,
    status_code=201,
)
def create_task_plan(
    conversation_id: int,
    payload: TaskPlanCreate,
    session: Session = Depends(get_session),
):
    """生成一份计划（US-403）。

    目前**不接模型提案**：内核循环（第 5 步）才持有网关。缺省走编排模板，
    这与「科研模式 = 替换计划模板」的衔接约定方向一致 —— 通用对话式规划随后补上。
    """
    _require_conversation(session, conversation_id)
    if task_plans_dao.has_running_plan(session, conversation_id):
        raise HTTPException(
            status_code=409,
            detail="该会话已有计划正在执行，执行期间不可重新规划（否则「当前计划」会有两个答案）",
        )

    try:
        template = planner.get_template(payload.template_id)
        plan = planner.build_plan(
            goal=payload.goal,
            mode=payload.mode,
            deterministic=payload.deterministic,
            template=template,
        )
    except PlanError as exc:
        raise _as_http(exc) from exc

    return task_plans_dao.create(
        session,
        conversation_id=conversation_id,
        steps=plan.to_steps_payload(),
        mode=plan.mode,
        deterministic=plan.deterministic,
        seed=plan.seed,
        title=plan.title,
        rationale=plan.rationale,
    )


@router.get(
    "/api/conversations/{conversation_id}/task-plans",
    response_model=list[TaskPlanOut],
)
def list_task_plans(
    conversation_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
):
    """会话的计划，新的在前。**列表第一条就是「当前计划」**（重规划会产生新行）。"""
    _require_conversation(session, conversation_id)
    return task_plans_dao.list_for_conversation(session, conversation_id, limit=limit)


@router.get("/api/task-plans/{plan_id}", response_model=TaskPlanOut)
def get_task_plan(plan_id: int, session: Session = Depends(get_session)):
    plan = task_plans_dao.get(session, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    return plan


@router.patch("/api/task-plans/{plan_id}", response_model=TaskPlanOut)
def update_task_plan(
    plan_id: int,
    payload: TaskPlanUpdate,
    session: Session = Depends(get_session),
):
    """人工修改与批准。

    两件事共用一个入口，但规则不同：

    - 改内容（``steps``/``title``/``rationale``）→ 只允许 ``draft``；``version`` 加一
    - ``status=approved`` → 只允许 ``draft → approved``；批准即冻结

    ``steps`` 里省略 ``status`` 的步骤会**沿用同名步骤的既有状态**，
    而不是被打回 ``pending``（见 ``PlanStepIn``）。
    """
    row = task_plans_dao.get(session, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="计划不存在")

    wants_content_edit = (
        payload.steps is not None
        or payload.title is not None
        or payload.rationale is not None
    )
    if payload.status is not None and payload.status != "approved":
        raise HTTPException(
            status_code=400,
            detail=f"该接口不接受 status={payload.status}；目前只支持 approved"
                   "（executing / done / failed 由内核在执行时写入）",
        )

    if wants_content_edit:
        plan = planner.Plan.from_row(row)
        try:
            planner.ensure_editable(row.status)
            merged = _merge_step_statuses(plan, payload.steps)
            plan = planner.revise_plan(
                plan, steps=merged, title=payload.title, rationale=payload.rationale,
            )
        except PlanError as exc:
            raise _as_http(exc) from exc
        row = task_plans_dao.update_content(
            session,
            plan_id,
            steps=plan.to_steps_payload(),
            title=plan.title,
            rationale=plan.rationale,
        )

    if payload.status == "approved":
        try:
            planner.ensure_approvable(row.status)
        except PlanError as exc:
            raise _as_http(exc) from exc
        row = task_plans_dao.set_status(session, plan_id, "approved")

    return row


def _merge_step_statuses(plan: planner.Plan, incoming) -> list[dict] | None:  # noqa: ANN001
    """把未提交状态的步骤接到既有状态上；新步骤一律 pending。"""
    if incoming is None:
        return None
    known = {step.id: step.status for step in plan.steps}
    merged: list[dict] = []
    for item in incoming:
        data = item.model_dump()
        if data["status"] is None:
            data["status"] = known.get(data["id"], "pending")
        merged.append(data)
    return merged
