from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.store.models import JOB_TERMINAL_STATUSES, Job, JobEvent


def create(
    session: Session,
    *,
    project_id: int,
    kind: str,
    stage_id: str | None = None,
    params: dict | None = None,
) -> Job:
    job = Job(
        project_id=project_id, kind=kind, stage_id=stage_id,
        status="queued", params=params or {},
    )
    session.add(job)
    session.flush()
    return job


def get(session: Session, job_id: int) -> Job | None:
    return session.get(Job, job_id)


def list_for_project(session: Session, project_id: int, limit: int = 50) -> list[Job]:
    return list(
        session.scalars(
            select(Job).where(Job.project_id == project_id)
            .order_by(Job.id.desc()).limit(limit)
        ).all()
    )


def list_by_status(session: Session, status: str) -> list[Job]:
    return list(session.scalars(select(Job).where(Job.status == status).order_by(Job.id)).all())


def mark_running(session: Session, job_id: int) -> Job | None:
    job = session.get(Job, job_id)
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.now(UTC)
    session.flush()
    return job


def finish(
    session: Session,
    job_id: int,
    *,
    status: str,
    run_id: int | None = None,
    error: str | None = None,
) -> Job | None:
    """写入终态（succeeded / failed / paused）。"""
    job = session.get(Job, job_id)
    if job is None:
        return None
    job.status = status
    if run_id is not None:
        job.run_id = run_id
    job.error = error
    job.finished_at = datetime.now(UTC)
    session.flush()
    return job


def is_terminal(job: Job | None) -> bool:
    return job is not None and job.status in JOB_TERMINAL_STATUSES


def request_cancel(session: Session, job_id: int) -> Job | None:
    """置协作式取消标记（US-407）。**不改作业状态**。

    只写标记、不动状态，是因为「已请求取消」与「已经取消」是两件事：请求之后作业
    还在跑（要在下一个安全点才停），把它当场写成 failed/paused 就会出现
    「台账说停了、线程还在写」的错乱 —— 那正是原先拒绝取消运行中作业的理由。
    状态由干活的那一方在真正停下时自己写（``_settle_succeeded``）。

    **必须有独立的 ``commit``**：循环在**另一条线程**里用**另一个 session** 读这个标记，
    不提交的话它永远读不到（与 US-405「消息必须先 commit 再入队」同一条约定）。
    """
    job = session.get(Job, job_id)
    if job is None:
        return None
    job.cancel_requested = True
    session.commit()
    return job


def cancel_requested(session: Session, job_id: int | None) -> bool:
    """读取消标记。``job_id`` 为 ``None``（单测里的无作业运行）→ 恒为 False。

    ⚠️ 必须是**列查询**，不能用 ``session.get(Job, …)``。标记由**另一个线程的另一个
    session** 写下（``JobRunner.request_stop`` 在 HTTP 线程，循环在工作线程），而
    ``session.get`` 会先命中身份映射：本项目所有 session 都是
    ``expire_on_commit=False``，那个对象提交后**不会失效**，于是这里永远读到当初那份
    ``cancel_requested=False``。表现是「按了停止，循环看不见」——用户看着「正在停止」，
    作业却一路跑到底。单测抓不到它，是因为那些用例都在**同一个 session** 里写和读，
    恰好从这条路的旁边绕了过去。
    """
    if job_id is None:
        return False
    # 只 SELECT 一列：不构造实体、不查身份映射，读到的就是库里的现值。
    return bool(session.scalar(select(Job.cancel_requested).where(Job.id == job_id)))


# ── 事件 ────────────────────────────────────────

def next_seq(session: Session, job_id: int) -> int:
    """作业内自增序号；单 worker 串行执行，不存在并发取号冲突。"""
    current = session.scalar(
        select(func.max(JobEvent.seq)).where(JobEvent.job_id == job_id)
    )
    return int(current or 0) + 1


def add_event(session: Session, *, job_id: int, type: str, payload: dict | None = None) -> JobEvent:
    event = JobEvent(
        job_id=job_id, seq=next_seq(session, job_id), type=type, payload=payload or {},
    )
    session.add(event)
    session.flush()
    return event


def events_after(session: Session, job_id: int, after_seq: int = 0,
                 limit: int = 500) -> list[JobEvent]:
    """取 seq 大于 after_seq 的事件，按 seq 升序——SSE 增量投递的读端。"""
    return list(
        session.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.seq > after_seq)
            .order_by(JobEvent.seq)
            .limit(limit)
        ).all()
    )


def last_event(session: Session, job_id: int) -> JobEvent | None:
    return session.scalar(
        select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.seq.desc()).limit(1)
    )


def count_events(session: Session, job_id: int) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(JobEvent).where(JobEvent.job_id == job_id)
        ) or 0
    )


# ── 启动自愈 ────────────────────────────────────

def recover_orphans(session: Session, *, reason: str = "进程中断") -> int:
    """把上次进程遗留的 running 作业标为 failed，并补一条 job.failed 事件。

    不做自愈的话，这些僵尸作业会让前端永远转圈。
    """
    orphans = list_by_status(session, "running")
    for job in orphans:
        job.status = "failed"
        job.error = reason
        job.finished_at = datetime.now(UTC)
        session.flush()
        add_event(session, job_id=job.id, type="job.failed",
                  payload={"error": reason, "recovered": True})
    session.flush()
    return len(orphans)
