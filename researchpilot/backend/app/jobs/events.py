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
LLM_CALL = "llm.call"  # 模型调用结算

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
