"""US-304 §4：受理接口与 SSE 流。

验收三条：受理 **P95 < 100ms**（耗时与任务时长无关）、SSE **端到端事件序列**、
``Last-Event-ID`` **断线续传**。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.api import jobs as jobs_api
from app.api.jobs import _event_stream, _sse
from app.main import create_app


@pytest.fixture
def client(engine, session_factory, ai_config):
    app = create_app()
    app.state.engine = engine
    app.state.session_factory = session_factory
    from app.agents.demo_stage import register_all
    from app.ai.budget import BudgetManager
    from app.ai.client import LlmGateway
    from app.ai.registry import ProviderRegistry
    from app.ai.routing import Router
    from app.orchestration.orchestrator import Orchestrator, StageRegistry

    ai_registry = ProviderRegistry.from_config(ai_config)
    router = Router.from_config(ai_config, ai_registry.providers_map())
    gateway = LlmGateway(ai_registry, router, BudgetManager(ai_config["ai"]["budget"]))
    app.state.ai_registry = ai_registry
    app.state.router = router
    app.state.gateway = gateway

    registry = StageRegistry()
    register_all(registry)
    app.state.orchestrator = Orchestrator(registry, gateway)
    with TestClient(app) as c:
        yield c


def _project(client, title: str = "受理接口") -> int:
    return client.post("/api/projects", json={"title": title, "goal": "G"}).json()["id"]


def _parse_sse(text: str) -> tuple[list[dict], list[int]]:
    """解析 SSE 报文，返回 (data 帧, id 列表)。心跳注释行（``: ping``）按规范忽略。"""
    frames: list[dict] = []
    ids: list[int] = []
    for line in text.splitlines():
        if line.startswith("id: "):
            ids.append(int(line[4:]))
        elif line.startswith("data: "):
            frames.append(json.loads(line[6:]))
    return frames, ids


def _read_stream(client, url: str, headers: dict | None = None) -> tuple[list[dict], list[int]]:
    """读完整条 SSE 流。

    ``TestClient``（Starlette 1.6）会把响应体整体缓冲后才返回，所以这里拿到的
    是「流结束后」的全文 —— 内容与顺序照样能钉死，返回本身也证明了流会正常关闭
    （不会永远挂着）。真正的增量投递由 ``test_stream_delivers_while_job_is_running``
    直接驱动生成器来验证。
    """
    resp = client.get(url, headers=headers or {})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    return _parse_sse(resp.text)


def _slow_provider(client, monkeypatch, seconds: float = 0.4) -> None:
    """让模型调用变慢：用来证明受理耗时与任务时长无关。"""
    provider = client.app.state.ai_registry.get("mock")
    real = provider.complete

    def slow(request):  # noqa: ANN001
        time.sleep(seconds)
        return real(request)

    monkeypatch.setattr(provider, "complete", slow)


class _FakeRequest:
    """直接驱动 SSE 生成器时冒充 ``Request``：只需要 ``app`` 与 ``is_disconnected``。

    ``TestClient`` 会把响应体缓冲到结束才返回，所以「边跑边吐」这件事只能靠在
    进程内逐帧消费生成器来证明。
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def is_disconnected(self) -> bool:
        return False


# ── 受理：不阻塞 ────────────────────────────────

def test_run_accepts_and_returns_job_id_immediately(client, monkeypatch, wait_job):
    _slow_provider(client, monkeypatch, 1.0)
    project_id = _project(client)

    started = time.perf_counter()
    resp = client.post(f"/api/projects/{project_id}/stages/S1/run")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert elapsed_ms < 100, f"受理耗时 {elapsed_ms:.1f}ms —— 说明仍在同步执行阶段"

    # 受理返回时阶段还没跑完，作业确实在后台
    snapshot = client.get(f"/api/jobs/{body['job_id']}").json()
    assert snapshot["status"] in ("queued", "running")

    settled = wait_job(client, body["job_id"], timeout=30)
    assert settled["status"] == "succeeded"
    assert settled["run_id"] is not None


def test_accept_latency_p95_stays_under_100ms(client, monkeypatch, wait_job):
    """P95 < 100ms：受理开销只跟请求本身有关，跟任务要跑多久无关。"""
    _slow_provider(client, monkeypatch, 0.1)
    project_id = _project(client)

    samples: list[float] = []
    job_ids: list[int] = []
    for _ in range(10):
        started = time.perf_counter()
        resp = client.post(f"/api/projects/{project_id}/stages/S1/run")
        samples.append((time.perf_counter() - started) * 1000)
        assert resp.status_code == 202
        job_ids.append(resp.json()["job_id"])

    p95 = sorted(samples)[int(len(samples) * 0.95) - 1]
    assert p95 < 100, f"受理 P95 = {p95:.1f}ms，样本 = {[round(s, 1) for s in samples]}"

    # 单 worker 串行：第 1 个还在跑
    assert client.get(f"/api/jobs/{job_ids[0]}").json()["status"] == "running"
    # 收尾，避免 teardown 时还有线程在写库
    assert wait_job(client, job_ids[-1], timeout=30)["status"] == "succeeded"


def test_pipeline_accept_runs_every_stage(client, wait_job):
    project_id = _project(client, "全链路受理")

    resp = client.post(f"/api/projects/{project_id}/pipeline/run")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    snapshot = wait_job(client, job_id, timeout=30)
    assert snapshot["status"] == "succeeded" and snapshot["kind"] == "pipeline"

    events = client.get(f"/api/jobs/{job_id}/events?limit=500").json()
    starts = [e for e in events if e["type"] == "stage.start"]
    assert [e["payload"]["stage_id"] for e in starts] == [f"S{i}" for i in range(1, 9)]
    assert len(client.get(f"/api/projects/{project_id}/runs").json()) == 8


def test_pipeline_accept_validates_requested_stage_subset(client, wait_job):
    project_id = _project(client, "子集")

    subset = client.post(
        f"/api/projects/{project_id}/pipeline/run", json={"stage_ids": ["S2", "S3"]}
    )
    assert subset.status_code == 202
    assert wait_job(client, subset.json()["job_id"], timeout=30)["status"] == "succeeded"
    assert len(client.get(f"/api/projects/{project_id}/runs").json()) == 2

    bad = client.post(
        f"/api/projects/{project_id}/pipeline/run", json={"stage_ids": ["SX"]}
    )
    assert bad.status_code == 400


# ── SSE ────────────────────────────────────────

def test_sse_stream_delivers_the_whole_event_sequence(client):
    """端到端：从 job.queued 一路到 job.succeeded，seq 稠密且与 id 对齐。"""
    project_id = _project(client, "SSE 端到端")
    job_id = client.post(f"/api/projects/{project_id}/stages/S1/run").json()["job_id"]

    frames, ids = _read_stream(client, f"/api/jobs/{job_id}/stream")

    assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))
    assert ids == [f["seq"] for f in frames]
    types = [f["type"] for f in frames]
    assert types[0] == "job.queued" and types[1] == "job.running"
    assert "stage.start" in types and "step" in types and "llm.call" in types
    assert types[-1] == "job.succeeded"
    # 终态之后流就关了，不会再挂着
    assert frames[-1]["payload"]["status"] == "succeeded"


def test_stream_delivers_while_job_is_running(client, monkeypatch):
    """增量投递：收到第一帧时作业还没跑完。

    TestClient 会把响应缓冲到结束，所以这里直接驱动 SSE 生成器（``_event_stream``）
    逐帧消费 —— 这是唯一能证明「不是跑完才一次性吐出来」的方式，而一次性
    吐出来正是 FIX-03 要消灭的体验。
    """
    _slow_provider(client, monkeypatch, 0.6)
    project_id = _project(client, "增量投递")
    job_id = client.post(f"/api/projects/{project_id}/stages/S1/run").json()["job_id"]

    async def scenario():
        frames: list[dict] = []
        status_at_first: str | None = None
        async for chunk in _event_stream(_FakeRequest(client.app), job_id, 0):
            batch, _ = _parse_sse(chunk)
            if not batch:
                continue
            frames.extend(batch)
            if status_at_first is None:
                status_at_first = client.get(f"/api/jobs/{job_id}").json()["status"]
        return frames, status_at_first

    frames, status_at_first = asyncio.run(scenario())

    assert status_at_first in ("queued", "running"), "第一帧到手时作业就已经结束了"
    assert frames[0]["type"] == "job.queued"
    assert frames[-1]["type"] == "job.succeeded"
    assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))


def test_sse_replays_from_after_seq_and_last_event_id(client):
    """断线续传：``id`` 即 seq，重连能只拿到缺的那些。"""
    project_id = _project(client, "SSE 续传")
    job_id = client.post(f"/api/projects/{project_id}/stages/S1/run").json()["job_id"]

    full, _ = _read_stream(client, f"/api/jobs/{job_id}/stream")
    assert len(full) > 3

    tail, _ = _read_stream(client, f"/api/jobs/{job_id}/stream?after_seq=1")
    assert [f["seq"] for f in tail] == [f["seq"] for f in full[1:]]

    resumed, _ = _read_stream(
        client, f"/api/jobs/{job_id}/stream", headers={"Last-Event-ID": "1"}
    )
    assert [f["seq"] for f in resumed] == [f["seq"] for f in full[1:]]

    # 订阅晚了、事件已被跳过：补一条收尾事件，然后正常关闭
    settled, _ = _read_stream(client, f"/api/jobs/{job_id}/stream?after_seq=9999")
    assert len(settled) == 1
    assert settled[0]["type"] == "job.settled"
    assert settled[0]["payload"]["status"] == "succeeded"


def test_sse_reports_failures_as_events_not_http_errors(client):
    project_id = _project(client, "SSE 失败")
    client.app.state.ai_registry.get("mock")._fail_times = 999
    job_id = client.post(f"/api/projects/{project_id}/stages/S1/run").json()["job_id"]

    frames, _ = _read_stream(client, f"/api/jobs/{job_id}/stream")
    types = [f["type"] for f in frames]
    assert "stage.failed" in types
    assert types[-1] == "job.failed"
    assert frames[-1]["payload"]["code"] == "LLM-UNAVAIL-001"


def test_polling_fallback_events_endpoint_shares_seq_semantics(client, wait_job):
    """SSE 不可用时前端退化为轮询，接口共用同一套 seq 语义。"""
    project_id = _project(client, "轮询回退")
    job_id = client.post(f"/api/projects/{project_id}/stages/S1/run").json()["job_id"]
    wait_job(client, job_id)

    events = client.get(f"/api/jobs/{job_id}/events").json()
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    tail = client.get(f"/api/jobs/{job_id}/events?after_seq={events[0]['seq']}").json()
    assert [e["seq"] for e in tail] == [e["seq"] for e in events[1:]]
    assert all(e["created_at"] for e in events)


# ── 台账与取消 ──────────────────────────────────

def test_project_job_ledger_lists_newest_first(client, wait_job):
    project_id = _project(client, "作业台账")
    first = client.post(f"/api/projects/{project_id}/stages/S1/run").json()["job_id"]
    wait_job(client, first)
    second = client.post(f"/api/projects/{project_id}/stages/S2/run").json()["job_id"]
    wait_job(client, second)

    rows = client.get(f"/api/projects/{project_id}/jobs").json()
    assert [r["id"] for r in rows] == [second, first]
    assert {r["kind"] for r in rows} == {"stage"}
    assert all(r["status"] == "succeeded" for r in rows)


def test_cancel_queued_job_but_not_running_one(client, monkeypatch, wait_job):
    _slow_provider(client, monkeypatch, 0.8)
    project_id = _project(client, "取消")
    running = client.post(f"/api/projects/{project_id}/stages/S1/run").json()["job_id"]
    queued = client.post(f"/api/projects/{project_id}/stages/S2/run").json()["job_id"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(f"/api/jobs/{running}").json()["status"] == "running":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("作业没有进入 running")

    # 跑起来的作业不硬打断：同步编排在线程里，中断只会留下写了一半的状态
    assert client.post(f"/api/jobs/{running}/cancel").status_code == 409
    cancelled = client.post(f"/api/jobs/{queued}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] is True

    snapshot = client.get(f"/api/jobs/{queued}").json()
    assert snapshot["status"] == "failed" and snapshot["error"] == "作业已取消"
    events = client.get(f"/api/jobs/{queued}/events").json()
    assert events[-1]["type"] == "job.failed" and events[-1]["payload"]["cancelled"] is True

    assert client.post(f"/api/jobs/{queued}/cancel").status_code == 409  # 已终态
    assert wait_job(client, running, timeout=30)["status"] == "succeeded"


def test_job_endpoints_404_on_unknown_targets(client):
    project_id = _project(client, "404")

    assert client.get("/api/jobs/999999").status_code == 404
    assert client.get("/api/jobs/999999/events").status_code == 404
    assert client.get("/api/jobs/999999/stream").status_code == 404
    assert client.post("/api/jobs/999999/cancel").status_code == 404
    assert client.get("/api/projects/999999/jobs").status_code == 404
    assert client.post("/api/projects/999999/stages/S1/run").status_code == 404
    assert client.post("/api/projects/999999/pipeline/run").status_code == 404
    assert client.post(f"/api/projects/{project_id}/stages/S99/run").status_code == 404


# ── 帧契约与收尾帧 ───────────────────────────────

def test_sse_frame_carries_only_id_and_data():
    """帧形状是契约：只有 ``id`` + ``data``，事件类型放在 ``data.type`` 里。

    一旦补上 ``event:`` 字段，浏览器只会触发同名监听器，前端的通用
    ``onmessage`` 就再也不响 —— 而前端订阅正是靠它一条通道收全部事件的。
    ``id`` 同时是续传游标，不能发成 ``id: None``。
    """
    frame = _sse(7, "step", {"kind": "thought"})
    assert frame == 'id: 7\ndata: {"seq": 7, "type": "step", "payload": {"kind": "thought"}}\n\n'
    assert "event:" not in frame
    assert "\n\n" in frame

    # 作业不存在：没有 seq 可当游标，只发 data
    missing = _sse(None, "job.not_found", {"error": "作业不存在"})
    assert missing.startswith("data: ")
    assert "id:" not in missing


def test_stream_emits_heartbeat_while_idle(client, monkeypatch):
    """空闲期必须有注释心跳，否则中间层会按空闲超时把连接悄悄掐掉。

    心跳走到 15s，测起来太慢；把阈值压到 0 逼它立刻发一帧。
    """
    monkeypatch.setattr(jobs_api, "HEARTBEAT_SECONDS", 0.0)
    monkeypatch.setattr(jobs_api, "POLL_INTERVAL_SECONDS", 0.01)
    # 造一个「永远在跑」的作业：轮询永远读到 running 且读不到新事件
    monkeypatch.setattr(
        jobs_api, "_poll",
        lambda factory, job_id, after_seq: (
            {"job_id": job_id, "status": "running", "kind": "stage",
             "stage_id": "S1", "run_id": None, "error": None},
            [],
        ),
    )

    async def scenario() -> list[str]:
        chunks: list[str] = []
        async for chunk in _event_stream(_FakeRequest(client.app), 1, 0):
            chunks.append(chunk)
            if chunk.startswith(": ping"):
                return chunks
            assert len(chunks) < 50, "心跳始终没出现，流在被空转"
        return chunks

    chunks = asyncio.run(scenario())
    assert chunks[-1] == ": ping\n\n"


def test_stream_emits_not_found_frame_for_missing_job(client):
    """作业不存在时给一帧明确的收尾帧再关流 —— 不能让订阅端一直挂着等。"""

    async def scenario() -> list[dict]:
        frames: list[dict] = []
        async for chunk in _event_stream(_FakeRequest(client.app), 999_999, 0):
            batch, _ = _parse_sse(chunk)
            frames.extend(batch)
        return frames

    frames = asyncio.run(scenario())
    assert len(frames) == 1
    assert frames[0]["type"] == "job.not_found"
    assert "作业不存在" in frames[0]["payload"]["error"]
