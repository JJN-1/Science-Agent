from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.store.models import TaskPlan


def create(
    session: Session,
    *,
    conversation_id: int,
    steps: list[dict],
    mode: str,
    deterministic: bool,
    seed: int | None,
    title: str = "",
    rationale: str = "",
    status: str = "draft",
) -> TaskPlan:
    """落一份新计划。

    参数是**拆开的原始值**而不是 ``planner.Plan`` 对象：store 层不该依赖内核层。
    转换在 API 层做（``plan.to_steps_payload()``），与 ``messages_dao`` 只收算好的
    ``tokens`` 是同一条分层约定。
    """
    plan = TaskPlan(
        conversation_id=conversation_id,
        version=1,
        status=status,
        mode=mode,
        deterministic=deterministic,
        seed=seed,
        title=title,
        rationale=rationale,
        steps=list(steps),
    )
    session.add(plan)
    session.flush()
    return plan


def get(session: Session, plan_id: int) -> TaskPlan | None:
    return session.get(TaskPlan, plan_id)


def list_for_conversation(
    session: Session, conversation_id: int, *, limit: int = 20,
) -> list[TaskPlan]:
    """会话的计划，新的在前。重规划会产生新行，所以「当前计划」= 列表第一条。"""
    return list(
        session.scalars(
            select(TaskPlan)
            .where(TaskPlan.conversation_id == conversation_id)
            .order_by(TaskPlan.id.desc())
            .limit(limit)
        ).all()
    )


def current_for_conversation(session: Session, conversation_id: int) -> TaskPlan | None:
    plans = list_for_conversation(session, conversation_id, limit=1)
    return plans[0] if plans else None


def has_running_plan(session: Session, conversation_id: int) -> bool:
    """是否已有计划正在执行 —— 执行期间不许重规划，否则「当前计划」有两个答案。"""
    return session.scalar(
        select(TaskPlan.id)
        .where(TaskPlan.conversation_id == conversation_id, TaskPlan.status == "executing")
        .limit(1)
    ) is not None


def update_content(
    session: Session,
    plan_id: int,
    *,
    steps: list[dict] | None = None,
    title: str | None = None,
    rationale: str | None = None,
) -> TaskPlan | None:
    """原地修改内容，``version`` 加一。

    ⚠️ ``version`` 是**修订计数**，不提供历史回放。这不是偷懒：
    能改的只有 ``draft``（批准即冻结），而执行时的内容由 ``kernel_checkpoints.snapshot``
    冻结 —— 草稿阶段的几次改动没有回放价值，执行时的内容有快照兜底。
    真要计划级历史，该加的是归档表，不是让 version 假装是版本。
    """
    plan = session.get(TaskPlan, plan_id)
    if plan is None:
        return None
    if steps is not None:
        plan.steps = list(steps)
    if title is not None:
        plan.title = title
    if rationale is not None:
        plan.rationale = rationale
    plan.version += 1
    session.flush()
    return plan


def save_progress(
    session: Session, plan_id: int, *, steps: list[dict] | None = None,
) -> TaskPlan | None:
    """执行期写回各步状态（US-405）。**不动 ``version``、不限状态**。

    与 ``update_content`` 分开是因为两者约束相反：

    - ``update_content`` 是**人工修改内容**：只在 ``draft`` 允许，且 ``version`` 加一
    - ``save_progress`` 是**内核推进执行**：只在 ``approved`` / ``executing`` 发生，
      而它改的只有各步 ``status`` —— 步骤的 id / title / intent / tool / params 一个字
      都不动，否则「批准即冻结」就失效了（执行中的计划被改了内容，检查点指不回当时那份）

    把两者合成一个函数，迟早会出现「内核顺手改了计划内容却把 version 加一」，
    或者「人工改内容绕过了 draft 限制」。
    """
    plan = session.get(TaskPlan, plan_id)
    if plan is None:
        return None
    if steps is not None:
        plan.steps = list(steps)
    session.flush()
    return plan


def set_status(session: Session, plan_id: int, status: str) -> TaskPlan | None:
    plan = session.get(TaskPlan, plan_id)
    if plan is None:
        return None
    plan.status = status
    session.flush()
    return plan
