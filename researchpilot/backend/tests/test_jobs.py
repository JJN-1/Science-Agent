"""US-304 / FIX-03：异步作业层与作业台账。

验收三条：**submit 立即返回**（受理不触碰编排层）、**事件按 seq 可增量取**
（SSE 的读端契约）、**孤儿自愈**（上次进程遗留的 running 不会永远转圈）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.agents.demo_stage import register_all
from app.jobs.events import (
    JOB_FAILED,
    JOB_PAUSED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    LLM_CALL,
    STAGE_FAILED,
    STAGE_PAUSED,
    STAGE_START,
    STAGE_SUCCEEDED,
    STEP,
    TERMINAL_EVENT_TYPES,
)
from app.jobs.runner import JobRunner
from app.orchestration.base import StageAgent
from app.orchestration.orchestrator import STAGE_ORDER, Orchestrator, StageRegistry
from app.store.dao import agents as agents_dao
from app.store.dao import jobs as jobs_dao
from app.store.dao import projects as projects_dao
from app.store.dao import runs as runs_dao
from app.store.models import JobEvent


@pytest.fixture
def orchestrator(gateway):
    registry = StageRegistry()
    register_all(registry)
    return Orchestrator(registry, gateway)


def _project(session, title: str = "作业项目"):
    """建项目并提交：runner 用的是自建 session，看不到本测试会话的未提交写。"""
    project = projects_dao.create(session, title=title, goal="图神经网络推荐")
    session.commit()
    return project


def _runner(session_factory, orchestrator) -> JobRunner:
    return JobRunner(session_factory, lambda: orchestrator)


def _types(session, job_id: int) -> list[str]:
    return [e.type for e in jobs_dao.events_after(session, job_id)]


def _reload(session, job_id: int):
    """跨 session 读作业：runner 用自己的 session 写库，本会话的 identity map 是旧快照。"""
    session.expire_all()
    return jobs_dao.get(session, job_id)


# ── 受理与执行解耦 ──────────────────────────────

def test_submit_returns_immediately_without_touching_orchestration(session, session_factory):
    """受理路径只写台账：编排层一次都不该被调用。

    这条断言就是 FIX-03 的核心 —— 旧实现里受理与执行是同一件事，
    POST /run 会一直阻塞到阶段跑完。
    """
    touched: list[int] = []

    def provider():
        touched.append(1)
        raise AssertionError("受理路径不得触碰编排层")

    runner = JobRunner(session_factory, provider)
    project = _project(session)
    job = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")

    assert job.status == "queued"
    assert runner.pending_count() == 1
    assert touched == []
    assert _types(session, job.id) == [JOB_QUEUED]
    assert _reload(session, job.id).started_at is None


def test_queued_event_carries_route_context(session, session_factory):
    runner = JobRunner(session_factory, lambda: None)
    project = _project(session)
    job = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")

    queued = jobs_dao.events_after(session, job.id)[0]
    assert queued.seq == 1
    assert queued.payload == {"kind": "stage", "stage_id": "S1", "project_id": project.id}


# ── 事件流：seq 稠密、可增量取 ──────────────────

def test_event_seq_is_dense_and_terminal_event_closes_the_stream(
    session, session_factory, orchestrator
):
    runner = _runner(session_factory, orchestrator)
    project = _project(session)
    job = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")
    assert runner.process_pending_once() == job.id

    events = jobs_dao.events_after(session, job.id)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    kinds = [e.type for e in events]
    assert kinds[:2] == [JOB_QUEUED, JOB_RUNNING]
    assert STAGE_START in kinds and STAGE_SUCCEEDED in kinds
    assert kinds[-1] in TERMINAL_EVENT_TYPES and kinds[-1] == JOB_SUCCEEDED

    fresh = _reload(session, job.id)
    assert fresh.status == "succeeded"
    assert fresh.started_at is not None and fresh.finished_at is not None
    assert runs_dao.get_run(session, fresh.run_id).status == "succeeded"


def test_events_are_retrievable_incrementally_by_seq(session, session_factory, orchestrator):
    """``events_after(after_seq)`` 就是 SSE 断线续传的读端。"""
    runner = _runner(session_factory, orchestrator)
    project = _project(session)
    job = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")
    runner.process_pending_once()

    events = jobs_dao.events_after(session, job.id)
    assert len(events) > 3

    tail = jobs_dao.events_after(session, job.id, after_seq=events[0].seq)
    assert [e.seq for e in tail] == [e.seq for e in events[1:]]
    assert jobs_dao.events_after(session, job.id, after_seq=events[-1].seq) == []

    resumed = jobs_dao.events_after(session, job.id, after_seq=1)
    assert resumed == events[1:]

    assert jobs_dao.count_events(session, job.id) == len(events)
    assert jobs_dao.last_event(session, job.id).seq == events[-1].seq


def test_steps_and_llm_calls_are_mirrored_into_event_stream(
    session, session_factory, orchestrator
):
    """``StageContext.record`` 是唯一收口 —— 轨迹写完必然有对应事件。"""
    runner = _runner(session_factory, orchestrator)
    project = _project(session)
    job = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")
    runner.process_pending_once()

    run_id = _reload(session, job.id).run_id
    steps = runs_dao.list_steps(session, run_id)
    events = jobs_dao.events_after(session, job.id)

    # 轨迹行与流内事件一一对应、同序：llm_call 走 llm.call 事件，其余走 step 事件
    mirrored = [e for e in events if e.type in (STEP, LLM_CALL)]
    assert [e.payload["kind"] if e.type == STEP else "llm_call" for e in mirrored] == [
        s.kind for s in steps
    ]
    step_events = [e for e in mirrored if e.type == STEP]
    assert [(e.payload["kind"], e.payload["content"]) for e in step_events] == [
        (s.kind, s.content) for s in steps if s.kind != "llm_call"
    ]
    assert all(e.payload["run_id"] == run_id for e in mirrored)

    llm_events = [e for e in events if e.type == LLM_CALL]
    assert len(llm_events) == 1
    assert llm_events[0].payload["provider"] == "mock"
    assert llm_events[0].payload["cached"] is False


def test_sync_path_without_job_id_stays_free_of_job_events(session, orchestrator):
    """没有 job_id 时事件层完全不介入：老调用点行为不变。"""
    project = _project(session)
    run_id = orchestrator.run_stage(session, project.id, "S1")

    assert runs_dao.get_run(session, run_id).status == "succeeded"
    assert session.scalar(select(func.count()).select_from(JobEvent)) == 0


# ── 失败与暂停 ──────────────────────────────────

def test_failing_stage_lands_job_failed_and_keeps_the_crash_site(
    session, session_factory, gateway
):
    """失败现场的第一责任人是 worker：轨迹 / failed_attempt / 终态事件都要在。"""

    class FailingStage(StageAgent):
        stage_id, agent_id, name = "S1", "tester", "失败阶段"

        def run(self, ctx):  # noqa: ANN001
            ctx.think("即将失败")
            raise RuntimeError("boom")

    registry = StageRegistry()
    registry.register(FailingStage())
    runner = _runner(session_factory, Orchestrator(registry, gateway))
    project = _project(session)
    job = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")
    runner.process_pending_once()

    fresh = _reload(session, job.id)
    assert fresh.status == "failed" and "boom" in fresh.error
    assert runs_dao.get_run(session, fresh.run_id).status == "failed"
    assert runs_dao.list_steps(session, fresh.run_id), "失败前的轨迹不能丢"

    kinds = _types(session, job.id)
    assert STAGE_FAILED in kinds and kinds[-1] == JOB_FAILED
    assert jobs_dao.last_event(session, job.id).payload["error"] == "boom"


def test_provider_error_carries_its_code_into_the_failed_event(
    session, session_factory, gateway
):
    """后端不可用要能被前端按 code 分流（提示用户去设置页配 Key 还是稍后重试）。"""
    from app.ai.base import ProviderUnavailable

    class BrokenStage(StageAgent):
        stage_id, agent_id, name = "S1", "tester", "后端不可用"

        def run(self, ctx):  # noqa: ANN001
            raise ProviderUnavailable("后端凭据缺失，请在设置页补全")

    registry = StageRegistry()
    registry.register(BrokenStage())
    runner = _runner(session_factory, Orchestrator(registry, gateway))
    project = _project(session)
    job = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")
    runner.process_pending_once()

    payload = jobs_dao.last_event(session, job.id).payload
    assert payload["status"] == "failed"
    assert payload["provider_error"] is True
    assert payload["code"] == "LLM-UNAVAIL-001"


def test_budget_pause_maps_to_job_paused_not_failed(session, session_factory, orchestrator):
    """预算熔断是「暂停待审批」，不是失败 —— 作业状态机必须如实反映。"""
    agents_dao.upsert(session, agent_id="scout", name="Scout", tier="plan",
                      budget_steps=0, budget_cost=100.0)
    project = _project(session, "预算暂停项目")
    runner = _runner(session_factory, orchestrator)
    job = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")
    runner.process_pending_once()

    fresh = _reload(session, job.id)
    assert fresh.status == "paused"
    assert runs_dao.get_run(session, fresh.run_id).status == "paused"

    kinds = _types(session, job.id)
    assert STAGE_PAUSED in kinds and kinds[-1] == JOB_PAUSED
    assert jobs_dao.last_event(session, job.id).payload["reason"] == "budget"


# ── pipeline ────────────────────────────────────

def test_pipeline_job_runs_every_registered_stage(session, session_factory, orchestrator):
    runner = _runner(session_factory, orchestrator)
    project = _project(session, "全链路作业")
    job = runner.submit(session, project_id=project.id, kind="pipeline")
    runner.process_pending_once()

    fresh = _reload(session, job.id)
    assert fresh.status == "succeeded"
    assert len(runs_dao.list_for_project(session, project.id)) == 8

    starts = [e for e in jobs_dao.events_after(session, job.id) if e.type == STAGE_START]
    assert [e.payload["stage_id"] for e in starts] == list(STAGE_ORDER)


def test_terminal_job_is_not_executed_twice(session, session_factory, orchestrator):
    runner = _runner(session_factory, orchestrator)
    project = _project(session)
    job = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")
    runner.process_pending_once()

    before = jobs_dao.count_events(session, job.id)
    runner.run_job(job.id)  # 重入：已是终态，必须原地返回
    assert jobs_dao.count_events(session, job.id) == before
    assert len(runs_dao.list_for_project(session, project.id)) == 1


# ── 真实 worker ─────────────────────────────────

def test_worker_loop_runs_jobs_off_the_event_loop_and_stops_cleanly(
    session, session_factory, orchestrator
):
    """走真实路径：起协程 → 编排在工作线程里跑 → stop() 干净退出。

    这条覆盖 ``process_pending_once`` 跳过的部分：``asyncio.create_task`` +
    ``anyio.to_thread`` 的接线。编排是同步 SQLAlchemy，跑错线程会把整个服务卡死。
    """
    import asyncio
    import time

    runner = _runner(session_factory, orchestrator)
    project = _project(session)
    job = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")

    def status() -> str | None:
        with session_factory() as probe:
            row = jobs_dao.get(probe, job.id)
            return row.status if row else None

    async def scenario() -> str | None:
        runner.start()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if status() in ("succeeded", "failed", "paused"):
                    return status()
                await asyncio.sleep(0.05)
            return status()
        finally:
            await runner.stop()

    assert asyncio.run(scenario()) == "succeeded"
    assert runner.pending_count() == 0
    assert runner.current_job_id() is None


# ── 取消与自愈 ──────────────────────────────────

def test_cancel_drops_a_queued_job_and_settles_it_failed(session, session_factory):
    runner = JobRunner(session_factory, lambda: None)
    project = _project(session)
    first = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")
    second = runner.submit(session, project_id=project.id, kind="stage", stage_id="S2")

    assert runner.cancel(first.id) is True
    assert runner.pending_count() == 1

    fresh = _reload(session, first.id)
    assert fresh.status == "failed"
    last = jobs_dao.last_event(session, first.id)
    assert last.type == JOB_FAILED and last.payload["cancelled"] is True
    assert _reload(session, second.id).status == "queued"
    assert runner.cancel(first.id) is False  # 已终态，不再重复处置


def test_recover_orphans_settles_jobs_left_running_by_a_crash(session, session_factory):
    """进程被杀留下的 running 作业必须自愈，否则前端永远转圈。"""
    runner = JobRunner(session_factory, lambda: None)
    project = _project(session, "孤儿作业")
    job = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")
    jobs_dao.mark_running(session, job.id)
    session.commit()

    assert jobs_dao.recover_orphans(session) == 1
    session.commit()

    fresh = _reload(session, job.id)
    assert fresh.status == "failed" and fresh.error == "进程中断"
    assert fresh.finished_at is not None
    last = jobs_dao.last_event(session, job.id)
    assert last.type == JOB_FAILED and last.payload["recovered"] is True

    assert jobs_dao.recover_orphans(session) == 0  # 幂等
    assert jobs_dao.list_by_status(session, "running") == []


def test_list_for_project_returns_newest_first(session, session_factory):
    runner = JobRunner(session_factory, lambda: None)
    project = _project(session)
    first = runner.submit(session, project_id=project.id, kind="stage", stage_id="S1")
    second = runner.submit(session, project_id=project.id, kind="stage", stage_id="S2")

    listed = jobs_dao.list_for_project(session, project.id)
    assert [j.id for j in listed] == [second.id, first.id]
    assert jobs_dao.get(session, 10**6) is None


# ── 跨语言契约（后端事件类型 ↔ 前端收尾集合）────────────

FRONTEND_CLIENT = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "client.ts"
BACKEND_APP = Path(__file__).resolve().parents[1] / "app"


def _frontend_stream_end_types() -> set[str]:
    source = FRONTEND_CLIENT.read_text(encoding="utf-8")
    block = re.search(r"STREAM_END_EVENT_TYPES\s*=\s*\[(.*?)\]", source, re.S)
    assert block, "前端 client.ts 里找不到 STREAM_END_EVENT_TYPES —— 契约常量被改名或删掉了"
    return set(re.findall(r"'([^']+)'", block.group(1)))


def _backend_emitted_job_types() -> set[str]:
    """扫后端源码里所有 ``"job.xxx"`` 字面量。

    不 import 常量再比对，是因为收尾帧（``job.settled`` / ``job.not_found``）是
    API 层直接拼进 SSE 的，没有对应的常量；扫源码才盖得住它们。
    """
    found: set[str] = set()
    for path in (BACKEND_APP / "jobs" / "events.py", BACKEND_APP / "api" / "jobs.py"):
        found |= set(re.findall(r'"(job\.[a-z_.]+)"', path.read_text(encoding="utf-8")))
    return found


def test_frontend_knows_every_terminal_event():
    """后端认定的终态事件，前端必须都知道「收到它就收工」。

    这条契约出过一次事：``job.settled``（订阅晚于终态时后端补播的收尾帧）一开始
    不在前端的收尾集合里，前端只能靠 ``onerror`` 重连两轮才发现作业早结束了 ——
    白白多等两秒，还多开一次连接。
    """
    assert set(TERMINAL_EVENT_TYPES) <= _frontend_stream_end_types()


def test_frontend_stream_end_types_all_exist_on_the_backend():
    """反向：前端不能凭空发明类型 —— 收尾集合里每一项都得是后端真会发的。"""
    emitted = _backend_emitted_job_types()
    assert emitted, "后端源码里没扫到任何 job.* 事件类型，扫描逻辑该更新了"
    unknown = _frontend_stream_end_types() - emitted
    assert not unknown, f"前端收尾集合里有后端不会发的类型：{sorted(unknown)}"
