from __future__ import annotations

from sqlalchemy.orm import Session

from app.store.dao import jobs as jobs_dao
from app.store.models import JobEvent

# ── 事件类型契约 ────────────────────────────────
# 前端按 type 分派渲染；新增类型请同步 frontend/src/api/types.ts
JOB_QUEUED = "job.queued"
JOB_RUNNING = "job.running"
JOB_SUCCEEDED = "job.succeeded"
JOB_FAILED = "job.failed"
JOB_PAUSED = "job.paused"

STAGE_START = "stage.start"
STAGE_SUCCEEDED = "stage.succeeded"
STAGE_PAUSED = "stage.paused"
STAGE_FAILED = "stage.failed"

STEP = "step"          # 轨迹步骤的镜像（think / decide / record）
LLM_START = "llm.start"  # 模型调用**发起**（等待期间的唯一进度信号）
LLM_CALL = "llm.call"  # 模型调用结算

# 内核层事件（Sprint 4）。**「模型叙述的步骤」与「系统执行的工具」分开命名**（D12）：
# 步骤种类用 note，工具执行才用 tool.*。同名会让「声称做了」与「真的做了」在界面上
# 不可分辨，而区分这两件事正是审计价值的全部来源。
PLAN_UPDATED = "plan.updated"    # 计划生成 / 人工修改（US-403）
TOOL_CALL = "tool.call"          # 工具调用发起（US-404/405；Sprint 4 第 4 步定名后启用）
TOOL_RESULT = "tool.result"      # 工具调用结算
ASSISTANT_DELTA = "assistant.delta"  # 助手流式增量（US-405）
APPROVAL_REQUIRED = "approval.required"  # 危险操作待批准（US-406）

# 收到其中之一即表示作业已结束，SSE 可以正常关闭
TERMINAL_EVENT_TYPES = (JOB_SUCCEEDED, JOB_FAILED, JOB_PAUSED)


def emit(session: Session, job_id: int, type: str, payload: dict | None = None) -> JobEvent:  # noqa: A002
    """写一条作业事件并**立即提交**（D2）。

    ``type`` 沿用事件日志的字段名（与 ``job_events.type`` 列一致），
    因此这里刻意遮蔽内建名，保持调用点可读性。

    提交是刻意的：SSE 读端在另一条连接上按 ``seq`` 取已提交行，不提交就看不见。
    代价是事件的可见性先于业务事务的最终结果 —— 可接受，因为作业状态机只有
    一个写者（单 worker 串行），不存在读到「半成品状态」的竞争。

    形状上保持 (session, job_id, type, payload) 不变，是为了让编排层与 DAO 层
    的调用点长得一样：接管这条链路的人不需要记住「哪一层要 commit」。
    """
    event = jobs_dao.add_event(session, job_id=job_id, type=type, payload=payload or {})
    session.commit()
    return event
