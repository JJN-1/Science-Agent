from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

import anyio
from sqlalchemy.orm import Session, sessionmaker

from app.ai.base import ProviderError
from app.jobs.events import (
    JOB_FAILED,
    JOB_PAUSED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    emit,
)
from app.observability.logging import get_logger
from app.store.dao import approvals as approvals_dao
from app.store.dao import jobs as jobs_dao
from app.store.dao import runs as runs_dao
from app.store.models import Job

logger = get_logger("jobs.runner")

#: 暂停原因（``job.paused`` 事件的 ``reason``）。前端按它决定弹哪种审批卡。
PAUSE_BUDGET = "budget"
#: run 是 paused、但名下找不到待批单时的兜底值。
#: 宁可显示一个「不知道」，也不要默认成 ``budget`` —— 猜错的话用户会去加预算，
#: 而真正该做的是去翻一眼审批单。
PAUSE_UNKNOWN = "unknown"


def _pause_reason(session: Session, run_id: int | None, status: str) -> str:
    """这次暂停是为了什么 —— 从**该 run 名下**那张待批单的 ``kind`` 反推。

    US-406 之后暂停有两种（预算熔断、危险操作待批），而这里的 ``reason`` 曾是写死的
    ``"budget"``。不修的话，「等你批准执行这条命令」在界面上会显示成「预算熔断」：
    用户去加预算，而真正要做的是看一眼那条命令。

    按 ``run_id`` 而不是 ``project_id`` 查：同一个项目随时可能挂着好几张单子，
    按项目查会答错「**这次**暂停是为了什么」。
    """
    if status != "paused" or run_id is None:
        return ""
    kinds = {row.kind for row in approvals_dao.list_for_run(session, run_id, "pending")}
    if not kinds:
        return PAUSE_UNKNOWN
    return kinds.pop() if len(kinds) == 1 else "+".join(sorted(kinds))



def _current_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class JobRunner:
    """异步作业层（FIX-03）：把「受理」与「执行」解耦。

    受理路径只写台账 + 一条 ``job.queued``，随即返回 ``job_id``；执行交给进程内
    单 worker 队列。**单 worker 是刻意的**：SQLite 只有一个写者，让多个阶段并行
    只会互相抢写锁，把 wait 时间换成 timeout 风险。要并行得先换库。

    执行跑在 ``anyio.to_thread`` 的工作线程里 —— 编排层是同步 SQLAlchemy，
    与事件循环同线程会把整个服务卡死。线程里自建 session，绝不复用请求 session：
    请求在受理那一刻就结束了。

    作业分三种：``stage`` / ``pipeline`` 归 ``Orchestrator``，``chat`` 归内核循环
    （US-405 / D3）。内核循环**复用这条通道而不是另开一条** ——
    SSE 断线续传、``Last-Event-ID``、取消、僵尸作业自愈都已经在这里验证过一遍了，
    再开一条执行通道等于把它们重做一遍，还会分裂成两套语义。
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        orchestrator_provider: Callable[[], Any],
        chat_handler: Callable[[Session, int, dict, int], int] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._orchestrator_provider = orchestrator_provider
        #: ``(session, project_id, params, job_id) -> run_id``。内核循环由装配方注入，
        #: 作业层不认识内核 —— 它只负责「按 kind 派活」，认识内核会让这一层跟着内核一起改。
        self._chat_handler = chat_handler
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._current_job_id: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = _current_loop()

    # ── 受理 ────────────────────────────────────

    def submit(
        self,
        session: Session,
        *,
        project_id: int,
        kind: str,
        stage_id: str | None = None,
        params: dict | None = None,
    ) -> Job:
        """受理一个作业：写台账（queued）+ 一条 ``job.queued`` 事件，立刻返回。

        这里**不碰编排层**，所以受理耗时与任务规模无关 —— 这正是 FIX-03 要修的
        东西：原先 ``POST /run`` 同步跑完整个阶段，分钟级任务直接撞 HTTP 超时。
        """
        job = jobs_dao.create(
            session, project_id=project_id, kind=kind, stage_id=stage_id, params=params,
        )
        emit(
            session, job.id, JOB_QUEUED,
            {"kind": kind, "stage_id": stage_id, "project_id": project_id},
        )
        self._enqueue(job.id)
        logger.info("job_submitted", job_id=job.id, kind=kind, stage_id=stage_id)
        return job

    def _enqueue(self, job_id: int) -> None:
        """入队。跨线程时必须走 ``call_soon_threadsafe``。

        ``asyncio.Queue.put_nowait`` 直接唤醒等待中的 getter，而唤醒是绑定事件循环的：
        从别的线程调用只会留下一个无人通知的 future，worker 于是永远等下去，
        作业就一直停在 queued。API 层同线程无此问题，但脚本、CLI 与测试会踩到。
        """
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._queue.put_nowait, job_id)
            return
        self._queue.put_nowait(job_id)

    def cancel(self, job_id: int) -> bool:
        """尽力而为的进程内取消，返回是否真的取消了。

        只对**尚未开跑**的作业生效：把它从队列里摘掉并直接落 failed。
        正在跑的作业不打断 —— 同步编排跑在工作线程里，Python 无法安全中断它，
        硬取消只会留下「线程还在写、台账已判死」的错乱状态。跨进程取消不在范围内。

        调用方约定：与事件循环同线程（API 层即如此）。它会就地改写队列，
        跨线程调用时入队/重排的唤醒语义不再成立。
        """
        if self._current_job_id == job_id:
            return False
        kept: list[int] = []
        found = False
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item == job_id and not found:
                found = True
                continue
            kept.append(item)
        for item in kept:
            self._queue.put_nowait(item)
        if not found:
            return False
        with self._session_factory() as session:
            job = jobs_dao.get(session, job_id)
            if job is None or jobs_dao.is_terminal(job):
                return False
            jobs_dao.finish(session, job_id, status="failed", error="作业已取消")
            emit(session, job_id, JOB_FAILED,
                 {"status": "failed", "error": "作业已取消", "cancelled": True})
        logger.info("job_cancelled", job_id=job_id)
        return True

    def pending_count(self) -> int:
        return self._queue.qsize()

    def current_job_id(self) -> int | None:
        return self._current_job_id

    # ── 生命周期 ────────────────────────────────

    def start(self) -> None:
        """启动单 worker 消费协程（lifespan 调用；重复调用无害）。"""
        if self._worker is not None and not self._worker.done():
            return
        self._loop = asyncio.get_running_loop()
        self._worker = asyncio.create_task(self._worker_loop(), name="job-worker")

    async def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker

    async def _worker_loop(self) -> None:
        logger.info("job_worker_started")
        try:
            while True:
                job_id = await self._queue.get()
                self._current_job_id = job_id
                task = asyncio.ensure_future(anyio.to_thread.run_sync(self.run_job, job_id))
                self._task = task
                try:
                    await task
                except Exception:
                    # run_job 自己兜底写终态；走到这里说明连兜底都挂了
                    logger.exception("job_worker_unhandled_error", job_id=job_id)
                finally:
                    self._task = None
                    self._current_job_id = None
        except asyncio.CancelledError:
            logger.info("job_worker_stopped")
            raise

    # ── 执行（同步，跑在工作线程）────────────────

    def run_job(self, job_id: int) -> None:
        """执行一个作业并把它推到终态。异常绝不静默：兜底写 failed + 事件。"""
        with self._session_factory() as session:
            job = jobs_dao.get(session, job_id)
            if job is None:
                logger.warning("job_missing", job_id=job_id)
                return
            if jobs_dao.is_terminal(job):
                logger.info("job_already_settled", job_id=job_id, status=job.status)
                return

            kind, project_id, stage_id = job.kind, job.project_id, job.stage_id
            params = dict(job.params or {})

            run_id: int | None = None
            # 整个执行段（含「标 running」与 job.running 事件）都在保护范围内：
            # 任何一步抛出去，作业都会永远停在 running，只能等下次重启被自愈捡回来。
            try:
                jobs_dao.mark_running(session, job_id)
                session.commit()
                emit(session, job_id, JOB_RUNNING, {"kind": kind, "stage_id": stage_id})
                logger.info("job_running", job_id=job_id, kind=kind, stage_id=stage_id)

                orchestrator = self._orchestrator_provider()
                if kind == "chat":
                    if self._chat_handler is None:
                        raise RuntimeError(
                            "收到 kind=chat 作业，但装配时没有注入 chat_handler"
                            "（内核循环未接线）"
                        )
                    run_id = self._chat_handler(session, project_id, params, job_id)
                elif kind == "pipeline":
                    run_ids = orchestrator.run_pipeline(
                        session, project_id, params.get("stage_ids"), job_id=job_id,
                    )
                    run_id = run_ids[-1] if run_ids else None
                else:
                    run_id = orchestrator.run_stage(
                        session, project_id, stage_id, job_id=job_id,
                    )
                status = self._terminal_status(session, run_id)
            except Exception as exc:
                self._settle_failed(session, job_id, exc, run_id,
                                    project_id=project_id, stage_id=stage_id)
                return
            self._settle_succeeded(session, job_id, status, run_id,
                                   reason=_pause_reason(session, run_id, status))

    def process_pending_once(self) -> int | None:
        """同步跑掉队列里的下一个作业，返回 job_id（无待办返回 None）。

        测试与诊断出口：不起事件循环、不起线程，让「受理 → 执行 → 终态」
        可以确定性断言。生产路径走 ``_worker_loop``。
        """
        try:
            job_id = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        self.run_job(job_id)
        return job_id

    # ── 终态 ────────────────────────────────────

    @staticmethod
    def _terminal_status(session: Session, run_id: int | None) -> str:
        """从 run 反推作业终态；run 状态未落在终态集合时按成功处理。"""
        run = runs_dao.get_run(session, run_id) if run_id is not None else None
        if run is None:
            return "succeeded"
        return run.status if run.status in ("succeeded", "paused", "failed") else "succeeded"

    def _settle_succeeded(self, session: Session, job_id: int, status: str,
                          run_id: int | None, reason: str = "") -> None:
        if status == "paused":
            self._write(
                session, job_id,
                lambda: jobs_dao.finish(session, job_id, status="paused", run_id=run_id),
                JOB_PAUSED,
                {"status": "paused", "run_id": run_id, "reason": reason or PAUSE_BUDGET},
            )
            logger.info("job_paused", job_id=job_id, run_id=run_id, reason=reason)
            return
        self._write(
            session, job_id,
            lambda: jobs_dao.finish(session, job_id, status="succeeded", run_id=run_id),
            JOB_SUCCEEDED,
            {"status": "succeeded", "run_id": run_id},
        )
        logger.info("job_succeeded", job_id=job_id, run_id=run_id)

    @staticmethod
    def _resolve_run_id(session: Session, project_id: int, stage_id: str | None,
                        known: int | None) -> int | None:
        """失败时 ``run_stage`` 抛了异常、没机会返回 run_id —— 从库里补回来。

        补得回来是因为 run 行在阶段开跑前就写好了（``create_run``）。少了这一步，
        失败作业的 ``run_id`` 是空的，前端点「失败现场」就没有轨迹可跳。
        """
        if known is not None:
            return known
        for run in runs_dao.list_for_project(session, project_id):  # 新 → 旧
            if stage_id is None or run.stage_id == stage_id:
                return run.id
        return None

    def _settle_failed(self, session: Session, job_id: int, exc: Exception,
                       run_id: int | None, *, project_id: int,
                       stage_id: str | None) -> None:
        """失败现场的第一责任人是 worker。

        Orchestrator 已经把 run=failed / checkpoint / failed_attempt 写进了 session，
        原先由 API 层特判「先 commit 再抛 503」把它们救回来；受理接口不再阻塞，
        这段责任随之搬到这里。
        """
        try:
            session.commit()
        except Exception:
            session.rollback()
        run_id = self._resolve_run_id(session, project_id, stage_id, run_id)
        payload = {"status": "failed", "run_id": run_id, "error": str(exc)}
        if isinstance(exc, ProviderError):
            payload["code"] = exc.code
            payload["provider_error"] = True
        self._write(
            session, job_id,
            lambda: jobs_dao.finish(session, job_id, status="failed", run_id=run_id, error=str(exc)),
            JOB_FAILED, payload,
        )
        logger.warning("job_failed", job_id=job_id, error=str(exc))

    def _write(self, session: Session, job_id: int, write_job: Callable[[], object],
               event_type: str, payload: dict) -> None:
        """写终态 + 终态事件；兜底失败时只记日志，不让兜底本身再抛出去。"""
        try:
            write_job()
            emit(session, job_id, event_type, payload)
        except Exception:
            session.rollback()
            logger.exception("job_settle_failed", job_id=job_id, event=event_type)
