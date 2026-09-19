from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_session
from app.api.schemas import JobEventOut, JobOut
from app.jobs.events import TERMINAL_EVENT_TYPES
from app.store.dao import jobs as jobs_dao
from app.store.dao import projects as projects_dao
from app.store.models import JOB_TERMINAL_STATUSES

router = APIRouter(tags=["jobs"])

# 轮询而非通知：事件以数据库为准（ADR-0003 / D1），读端只认已提交行。
# 本地 SQLite 的一次 SELECT 是微秒级，0.25s 的间隔足以让进度看起来是连续的。
POLL_INTERVAL_SECONDS = 0.25
HEARTBEAT_SECONDS = 15.0
# 兜底上限：任何异常情况下协程都必须能退出，否则连接会一直挂着。
STREAM_MAX_SECONDS = 1800.0
EVENT_BATCH = 200


def _snapshot(job) -> dict:  # noqa: ANN001
    return {
        "job_id": job.id,
        "status": job.status,
        "kind": job.kind,
        "stage_id": job.stage_id,
        "run_id": job.run_id,
        "error": job.error,
    }


def _poll(factory: sessionmaker, job_id: int, after_seq: int) -> tuple[dict | None, list[dict]]:
    """读一批已提交事件（在线程里跑，避免同步 IO 卡住事件循环）。

    返回纯 dict 而不是 ORM 实例：session 一关，实例就 detached 了，
    把「能不能读属性」这种隐式依赖留在流式协程里迟早出事。
    """
    with factory() as session:
        job = jobs_dao.get(session, job_id)
        if job is None:
            return None, []
        events = jobs_dao.events_after(
            session, job_id, after_seq=after_seq, limit=EVENT_BATCH,
        )
        return _snapshot(job), [
            {"seq": e.seq, "type": e.type, "payload": e.payload} for e in events
        ]


def _sse(seq: int | None, event_type: str, payload: dict) -> str:
    """SSE 帧：``id`` 供 Last-Event-ID 续传，类型放在 data 里。

    刻意**不写 ``event:`` 字段**：一旦写了，浏览器只会触发同名监听器，
    通用的 ``onmessage`` 就再也不响了。前端需要的是一条「什么都能收到」的流，
    而不是给每个事件类型各注册一个监听器。
    """
    data = json.dumps(
        {"seq": seq, "type": event_type, "payload": payload}, ensure_ascii=False,
    )
    lines = []
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


async def _event_stream(request: Request, job_id: int, after_seq: int) -> AsyncIterator[str]:
    factory: sessionmaker = request.app.state.session_factory
    cursor = max(after_seq, 0)
    started = time.monotonic()
    last_beat = started
    while True:
        if await request.is_disconnected():
            return
        snapshot, events = await run_in_threadpool(_poll, factory, job_id, cursor)
        if snapshot is None:
            yield _sse(None, "job.not_found", {"error": "作业不存在"})
            return

        # 注意：作业状态是 ``succeeded``，事件类型是 ``job.succeeded``——两套名字不能混用。
        terminal = snapshot["status"] in JOB_TERMINAL_STATUSES
        for event in events:
            cursor = event["seq"]
            yield _sse(event["seq"], event["type"], event["payload"])
        # 终态事件已投递即收尾：作业结束了，流没有继续存在的理由。
        if events and events[-1]["type"] in TERMINAL_EVENT_TYPES:
            return
        if terminal and not events:
            # 作业在订阅之前就结束了，且事件已被 after_seq 跳过。
            yield _sse(cursor, "job.settled", snapshot)
            return
        if time.monotonic() - started > STREAM_MAX_SECONDS:
            return

        now = time.monotonic()
        if now - last_beat >= HEARTBEAT_SECONDS:
            last_beat = now
            yield ": ping\n\n"
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@router.get("/api/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, session: Session = Depends(get_session)):
    job = jobs_dao.get(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="作业不存在")
    return job


@router.get("/api/jobs/{job_id}/events", response_model=list[JobEventOut])
def list_job_events(job_id: int, after_seq: int = 0, limit: int = EVENT_BATCH,
                    session: Session = Depends(get_session)):
    """事件列表：SSE 不可用时的轮询回退接口（与流共用同一套 seq 语义）。"""
    if jobs_dao.get(session, job_id) is None:
        raise HTTPException(status_code=404, detail="作业不存在")
    return jobs_dao.events_after(session, job_id, after_seq=after_seq, limit=limit)


@router.get("/api/jobs/{job_id}/stream")
async def stream_job(
    job_id: int,
    request: Request,
    after_seq: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """订阅作业事件流（text/event-stream）。

    续传：浏览器断线重连会自动带上 ``Last-Event-ID``；显式传 ``?after_seq=``
    时以查询参数为准（便于手工重放与测试）。事件投递到终态为止，之后正常关闭。
    """
    factory: sessionmaker = request.app.state.session_factory
    with factory() as session:
        if jobs_dao.get(session, job_id) is None:
            raise HTTPException(status_code=404, detail="作业不存在")

    start = after_seq
    if start <= 0 and last_event_id:
        try:
            start = max(int(last_event_id), 0)
        except ValueError:
            start = 0

    return StreamingResponse(
        _event_stream(request, job_id, start),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 反代不缓冲，否则事件会被攒着一起发
        },
    )


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int, request: Request, session: Session = Depends(get_session)) -> dict:
    job = jobs_dao.get(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="作业不存在")
    if jobs_dao.is_terminal(job):
        raise HTTPException(status_code=409, detail=f"作业已结束（{job.status}）")
    runner = request.app.state.job_runner
    if not runner.cancel(job_id):
        # 正在执行中的作业不会被硬打断：同步编排跑在工作线程里，
        # 强行取消只会留下「线程还在写、台账已判死」的错乱状态。
        raise HTTPException(status_code=409, detail="作业正在执行，无法取消")
    return {"job_id": job_id, "status": "failed", "cancelled": True}


@router.get("/api/projects/{project_id}/jobs", response_model=list[JobOut])
def list_project_jobs(project_id: int, limit: int = 50,
                      session: Session = Depends(get_session)):
    if projects_dao.get(session, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return jobs_dao.list_for_project(session, project_id, limit=limit)
