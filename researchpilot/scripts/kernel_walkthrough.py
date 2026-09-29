#!/usr/bin/env python
"""US-408 内核走查：3 个工具完成一个 ≥5 步的真实任务，并把整条事件流摊开。

这条脚本同时给出 G2 闸口第 1、2、7 三条的原始输出：

1. **第 1 条** —— `read_file` / `write_file` / `run_command` 三个工具能不能完成一个
   ≥5 步的真实任务？判定标准是「沙箱里真的多出一个产物文件、内容可读回」，
   而不是「工具返回了 ok」。
2. **第 2 条** —— 全程是否流式可见？判定四件事：事件序号稠密、首帧远早于全程结束
   （不是结束后一次性回放）、每条 `tool.call` 都先于它自己的 `tool.result`、
   跨「暂停 → 批准 → 恢复」两次作业能拼成**一条**完整任务序列。
3. **第 7 条** —— `deterministic` 的 golden case：同一输入跑两次，计划步骤与
   工具调用序列逐项相等（`temperature=0` + 缓存关闭，否则第二次只是缓存命中）。

它和 `slice_walkthrough.py` 的分工：那条走「阶段」通道（S1 等），这条走**内核会话**
通道，且**自带一份临时数据目录**——不碰 `%APPDATA%\\ResearchPilot` 里的真实数据。

用法（在 backend 下跑即可，脚本自己起 uvicorn，不用另开终端）：

    cd backend && uv run python ../scripts/kernel_walkthrough.py

    # 事件序列默认存档到 <仓库根>/tmp/kernel_walkthrough_events.json
    cd backend && uv run python ../scripts/kernel_walkthrough.py --out D:/events.json

退出码 0 = 全部断言通过；非 0 = 有硬性失败（末尾逐条列出）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[2]                      # 科研Agent/
BACKEND_DIR = HERE.parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

TERMINAL_JOB_STATUSES = ("succeeded", "failed", "paused")
TIERS = ("extract", "plan", "critique", "synthesize", "write")

#: 走查的「真实任务」：写一个最小实验脚本 → 真跑它 → 读回它写出的产物。
#: 三个工具各用到：`write_file` 落脚本、`run_command` 跑脚本、`read_file` 读产物；
#: 外加 `list_dir` 起步（真实任务总是先看一眼目录）。
TOY_SOURCE = (
    "import json\n"
    "import pathlib\n"
    "\n"
    "out = pathlib.Path('experiments/toy_result.json')\n"
    "payload = {'ok': True, 'n': 42, 'note': 'minimal in-sandbox experiment'}\n"
    "out.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')\n"
    "print('toy experiment done')\n"
)

CHECKS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  [{'ok' if ok else '!!'}] {name}{f' — {detail}' if detail else ''}", flush=True)


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ── 夹具：临时数据目录与配置 ──────────────────────

def task_script(python_exe: str) -> list[dict]:
    """第 N 次模型调用吐第 N 条调用，用尽后回到普通文本作答（于是会话自己结束）。"""
    return [
        {"name": "list_dir", "arguments": {"path": "."}},
        {"name": "write_file", "arguments": {
            "path": "experiments/toy_experiment.py", "content": TOY_SOURCE}},
        {"name": "read_file", "arguments": {"path": "experiments/toy_experiment.py"}},
        # 危险档：这一步会让内核**整轮挂起**等人批准 —— 真实任务本来就有这一步
        {"name": "run_command", "arguments": {
            "argv": [python_exe, "experiments/toy_experiment.py"], "timeout_s": 60}},
        {"name": "read_file", "arguments": {"path": "experiments/toy_result.json"}},
    ]


def write_temp_config(
    root: Path, *, tool_script: list[dict], response: str, provider: str = "walkmock",
) -> None:
    """写一份**只覆盖必要键**的临时配置（`config/default.yaml` 仍是底座）。

    - 走查用独立 provider 名（`walkmock`）：默认的 `mock` 是给阶段通道用的，
      两处共用同一个 provider 会让 `MockProvider._calls` 这个实例计数器互相干扰
      （第二次跑从脚本中间起跳，表现成「模型第一轮就不调工具」）。
    - **缓存显式关闭**：第 7 条的 golden case 要证明的是「模型路径本身确定」，
      开着缓存的话第二次直接命中缓存，等于把一个恒真命题写成通过。
    """
    import yaml  # noqa: PLC0415 — 只为写夹具，不进主流程

    cfg = {
        "ai": {
            "providers": {provider: {
                "type": "mock", "models": ["m1"], "vendor": "mock",
                "capabilities": ["json_object", "tools"],
                "response": response, "tool_script": tool_script,
            }},
            "routing": {tier: [{"provider": provider, "model": "m1"}] for tier in TIERS},
            "cache": {"enabled": False},
            "budget": {"project_total": 50.0, "project_daily": 10.0},
        },
    }
    (root / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )


class Server:
    """把一个真实 uvicorn 跑在后台线程里。

    为什么不用 `TestClient`：第 2 条要证明的是「事件真的**流式**到达客户端」，
    ASGI 直连会把 HTTP 与 SSE 一起搬到进程内，测出来的是「服务器逻辑能产出帧」，
    而不是「帧能从网络上一帧一帧地出来」。第 1 条要的「真实任务」也因此跑在真实的
    HTTP + 真实的工作线程上。
    """

    def __init__(self, app, port: int) -> None:
        import uvicorn  # noqa: PLC0415

        self._config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", access_log=False,
        )
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None
        self.port = port

    def start(self, *, timeout: float = 30.0) -> None:
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.started:
                return
            time.sleep(0.05)
        raise AssertionError("uvicorn 未在 30s 内就绪")

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15)


# ── HTTP 与事件收集 ──────────────────────────────

def await_job(client, job_id: int, timeout: float = 60.0) -> dict:
    """等一个作业到终态：受理与执行解耦之后，断言必须自己等（FIX-03 的契约）。"""
    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/jobs/{job_id}").json()
        if snapshot["status"] in TERMINAL_JOB_STATUSES:
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"作业 {job_id} 未在 {timeout}s 内结束：{snapshot}")


def collect_stream(client, job_id: int, *, timeout: float = 120.0) -> list[dict]:
    """订阅 SSE 并把每一帧**带到达时刻**记下来。

    `_at_ms` 是这一条证据的关键：没有它，「流式」与「结束后一次性回放」在数据上
    长得一模一样（事件序号一样稠密、内容一样正确）。
    """
    frames: list[dict] = []
    t0 = time.perf_counter()
    with client.stream("GET", f"/api/jobs/{job_id}/stream", timeout=timeout) as resp:
        if not frames:
            record(f"job {job_id} 流是 text/event-stream",
                   resp.headers.get("content-type", "").startswith("text/event-stream"),
                   resp.headers.get("content-type", ""))
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            frame = json.loads(line[6:])
            frame["_at_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            frames.append(frame)
            print(f"    +{frame['_at_ms']:8.0f}ms  #{frame['seq']:<3} {frame['type']}"
                  f"{_brief(frame)}", flush=True)
    return frames


def _brief(frame: dict) -> str:
    payload = frame.get("payload") or {}
    kind = frame["type"]
    if kind in ("tool.call", "tool.result"):
        extra = f" {payload.get('tool')}"
        if kind == "tool.call":
            extra += f" {json.dumps(payload.get('args'), ensure_ascii=False, sort_keys=True)}"
        else:
            extra += f" ok={payload.get('ok')} {payload.get('duration_ms')}ms"
        return extra
    if kind == "plan.updated":
        return f" status={payload.get('status')}"
    if kind == "assistant.delta":
        text = " ".join(str(payload.get("text") or "").split())
        return f" {text[:70]}{'…' if len(text) > 70 else ''}"
    if kind.startswith("job."):
        return f" status={payload.get('status')} reason={payload.get('reason') or '-'}"
    return ""


def dispatch_chat_job(app, *, project_id: int, conversation_id: int, agent_id: str) -> int:
    """派一个带 `agent_id` 的会话作业。

    ⚠️ 这一步是**进程内**投递，不是 HTTP：`POST /api/conversations/{id}/messages`
    刻意不暴露 `agent_id`（不暴露 = 客户端无法把自己提权到 `executor`，而
    `executor` 手里有 `run_command`）。要动用沙箱工具就必须**显式提权**，
    这条约定在走查里也照样写明，不给自己开后门。
    """
    session = app.state.session_factory()
    try:
        job = app.state.job_runner.submit(
            session, project_id=project_id, kind="chat",
            params={"conversation_id": conversation_id, "agent_id": agent_id},
        )
        session.commit()
        return int(job.id)
    finally:
        session.close()


# ── 第 7 条：golden 序列的标准化 ─────────────────

def normalize(frames: list[dict]) -> list[str]:
    """把事件序列压成「可逐项比较」的形状（丢掉 id / 时间 / 耗时这类合法差异）。"""
    out: list[str] = []
    for frame in frames:
        kind = frame["type"]
        payload = frame.get("payload") or {}
        if kind == "tool.call":
            out.append(f"tool.call {payload.get('tool')} "
                       f"{json.dumps(payload.get('args'), ensure_ascii=False, sort_keys=True)}")
        elif kind == "tool.result":
            out.append(f"tool.result {payload.get('tool')} ok={payload.get('ok')}")
        elif kind == "assistant.delta":
            out.append(f"assistant.delta {payload.get('text')}")
        elif kind == "plan.updated":
            steps = tuple((s.get("id"), s.get("status")) for s in payload.get("steps") or [])
            out.append(f"plan.updated {payload.get('status')} {steps}")
        elif kind.startswith("llm."):
            # 模型调用本身带耗时与 token 数 —— 只留「发生过一次」这件事
            out.append(kind)
        elif kind.startswith("job."):
            out.append(f"{kind} reason={payload.get('reason') or '-'}")
    return out


def fresh_gateway(app, script: list[dict], text: str):
    """造一个全新的、按脚本吐工具调用的网关（每一步都新建，见 `write_temp_config`）。"""
    from app.ai.budget import BudgetManager
    from app.ai.client import LlmGateway
    from app.ai.registry import ProviderRegistry
    from app.ai.routing import Router

    cfg = {"ai": {
        "providers": {"goldenmock": {
            "type": "mock", "models": ["m1"], "vendor": "mock",
            "capabilities": ["json_object", "tools"],
            "response": text, "tool_script": script,
        }},
        "routing": {tier: [{"provider": "goldenmock", "model": "m1"}] for tier in TIERS},
        "budget": {"project_total": 50.0},
    }}
    registry = ProviderRegistry.from_config(cfg)
    return LlmGateway(
        registry,
        Router.from_config(cfg, registry.providers_map()),
        BudgetManager(cfg["ai"]["budget"]),
        {"enabled": False},
    )


# ── 主流程 ──────────────────────────────────────

def main() -> int:  # noqa: C901 — 走查脚本按验收条目直线铺开，比拆成小函数更好读
    parser = argparse.ArgumentParser(description="US-408 内核走查（G2 第 1、2、7 条）")
    parser.add_argument("--port", type=int, default=0, help="后端端口（0 = 自动挑一个空闲端口）")
    parser.add_argument("--out", default=None, help="事件序列存档路径（默认 <仓库根>/tmp/...）")
    parser.add_argument("--keep-tmp", action="store_true", help="保留临时数据目录（排查用）")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else REPO_ROOT / "tmp" / "kernel_walkthrough_events.json"
    workdir = Path(tempfile.mkdtemp(prefix="rp-walkthrough-"))
    print(f"临时数据目录：{workdir}", flush=True)

    os.environ["RESEARCHPILOT_DATA_DIR"] = str(workdir)
    response_text = "任务完成：脚本已落盘、真实跑过、产物已读回。"
    write_temp_config(workdir, tool_script=task_script(sys.executable), response=response_text)

    import httpx  # noqa: PLC0415

    from app.main import create_app  # noqa: PLC0415 — 必须在设好数据目录之后

    app = create_app()
    port = args.port or _free_port()
    server = Server(app, port)
    server.start()
    base = f"http://127.0.0.1:{port}"
    client = httpx.Client(
        base_url=base,
        timeout=httpx.Timeout(30.0, read=180.0),
        # SSE 的每一帧都要立刻到手上：缓冲会把「流式」重新变成「一次性」
        headers={"Accept": "text/event-stream"},
    )

    archive: dict = {"base": base, "goal": None, "jobs": [], "events": [], "tool_calls": []}
    try:
        step("0. 后端可达（这一条走的是真实 HTTP，不是 ASGI 直连）")
        health = client.get("/api/health").json()
        record("GET /api/health", health.get("status") == "ok", str(health))

        step("1. 建项目与会话")
        project = client.post(
            "/api/projects", json={"title": "内核走查", "goal": "跑通一次最小实验"},
        ).json()
        conversation = client.post(
            "/api/conversations", json={"project_id": project["id"]},
        ).json()
        archive["project_id"] = project["id"]
        archive["conversation_id"] = conversation["id"]
        record("POST /api/projects", bool(project.get("id")), f"project_id={project['id']}")
        record("POST /api/conversations", bool(conversation.get("id")),
               f"conversation_id={conversation['id']}")

        step("2. 派会话作业：提权到 executor（沙箱工具只挂在它身上）")
        job_id = dispatch_chat_job(
            app, project_id=project["id"], conversation_id=conversation["id"],
            agent_id="executor",
        )
        record("作业已受理", job_id > 0, f"job_id={job_id}")

        step("3. 订阅事件流（第 1 段：跑到危险操作前）")
        first = collect_stream(client, job_id)
        first_status = await_job(client, job_id)["status"]
        archive["jobs"].append({"job_id": job_id, "status": first_status,
                                "event_count": len(first)})

        # ⚠️ 危险操作（run_command）会让**整轮挂起**等人批准 —— 这是设计的一部分，
        # 不是走查的意外。紧接着走一遍真实的人批链（界面上的批准卡就是这两个接口）。
        resumed_frames: list[dict] = []
        resumed_job_id: int | None = None
        if first_status == "paused":
            step("4. 危险操作挂起 → 走真实审批接口批准 → 恢复执行")
            last = first[-1] if first else {}
            reason = (last.get("payload") or {}).get("reason")
            record("暂停原因是等待批准（不是预算）", reason == "dangerous", f"reason={reason}")

            cards = client.get("/api/approvals", params={"project_id": project["id"]}).json()
            pending = [c for c in cards if c["kind"] == "dangerous"]
            record("库里有待批单", len(pending) == 1,
                   f"{[c['kind'] for c in cards]}")
            approval_id = pending[0]["id"]
            tools = [c["tool"] for c in pending[0]["detail"]["pending"]]
            record("待批的正是那条命令", tools == ["run_command"], str(tools))

            approved = client.post(f"/api/approvals/{approval_id}/approve",
                                   json={"note": "走查批准"})
            record("POST /api/approvals/{id}/approve → 200", approved.status_code == 200,
                   approved.text[:120])
            resumed_job_id = approved.json().get("job_id")
            record("批准派出了恢复作业", bool(resumed_job_id), f"job_id={resumed_job_id}")

            resumed_frames = collect_stream(client, int(resumed_job_id))
            resumed_status = await_job(client, int(resumed_job_id))["status"]
            archive["jobs"].append({"job_id": resumed_job_id, "status": resumed_status,
                                    "event_count": len(resumed_frames)})
            record("恢复后跑完", resumed_status == "succeeded", f"status={resumed_status}")
        else:
            record("任务未暂停（脚本里没有危险操作？）", False, f"status={first_status}")

        frames = first + resumed_frames
        archive["events"] = frames

        step("5. 第 1 条：3 个工具完成 ≥5 步真实任务")
        session = app.state.session_factory()
        try:
            from app.store.dao import tool_calls as tool_calls_dao

            rows = tool_calls_dao.list_for_conversation(session, conversation["id"])
            calls = [{"tool": r.tool_name, "args": r.args, "status": r.status,
                      "permission": r.permission, "duration_ms": r.duration_ms,
                      "error": r.error} for r in rows]
        finally:
            session.close()
        archive["tool_calls"] = calls
        used = [c["tool"] for c in calls]
        record("工具调用次数 ≥ 5", len(calls) >= 5, f"{len(calls)} 次：{used}")
        for name in ("read_file", "write_file", "run_command"):
            record(f"用到了 {name}", name in used, f"permission="
                   f"{next((c['permission'] for c in calls if c['tool'] == name), '-')}")
        record("全部调用成功", all(c["status"] == "ok" for c in calls),
               " / ".join(f"{c['tool']}={c['status']}" for c in calls))

        artifact = (workdir / "workspace" / f"project-{project['id']}"
                    / "experiments" / "toy_result.json")
        record("沙箱里真的有产物文件", artifact.exists(), str(artifact))
        if artifact.exists():
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            record("产物内容与脚本写的一致", payload.get("ok") is True and payload.get("n") == 42,
                   json.dumps(payload, ensure_ascii=False))
            record("产物被 read_file 读回",
                   any(c["tool"] == "read_file" and "toy_result" in json.dumps(c["args"])
                       for c in calls))

        step("6. 第 2 条：全程流式可见")
        for chunk, label in ((first, f"job {job_id}"), (resumed_frames, f"job {resumed_job_id}")):
            if not chunk:
                continue
            seqs = [f["seq"] for f in chunk]
            record(f"{label} 事件序号稠密（1..N 无缺口）",
                   seqs == list(range(1, len(seqs) + 1)), f"seq={seqs[:4]}…{seqs[-1]}")
            record(f"{label} 末帧是终态事件",
                   chunk[-1]["type"] in ("job.succeeded", "job.failed", "job.paused"),
                   chunk[-1]["type"])
            # 首帧远早于全程结束 = 帧是**边跑边到**的，不是结束后一次性回放
            total = chunk[-1]["_at_ms"]
            record(f"{label} 进度不是最后一次性吐出",
                   chunk[0]["_at_ms"] < max(total * 0.5, 1.0),
                   f"首帧 +{chunk[0]['_at_ms']}ms / 终帧 +{total}ms")

        calls_in_stream = [f for f in frames if f["type"] == "tool.call"]
        results_in_stream = [f for f in frames if f["type"] == "tool.result"]
        record("流里有 tool.call（不只是库里有记录）", bool(calls_in_stream),
               f"{len(calls_in_stream)} 条")
        record("流里有 tool.result", bool(results_in_stream), f"{len(results_in_stream)} 条")
        paired = True
        for frame in calls_in_stream:
            call_id = frame["payload"].get("call_id")
            later = [f for f in frames
                     if f["type"] == "tool.result"
                     and f["payload"].get("call_id") == call_id
                     and f["_at_ms"] >= frame["_at_ms"]]
            if not later:
                paired = False
                record(f"call {call_id} 有对应结果", False, "没有等到 tool.result")
        record("每条 tool.call 都先于它自己的 tool.result", paired)
        record("事件总数 ≥ 工具调用数（每一步都留下了痕迹）",
               len(frames) >= len(calls), f"{len(frames)} 帧 / {len(calls)} 次调用")

        step("7. 第 7 条：deterministic 的 golden case（同一输入跑两次，逐项相等）")
        golden: dict = {"plan": {}, "kernel": {}}

        # 7a 计划器：`deterministic=True` 完全不问模型，两次的计划必须逐字相同
        plan_pairs = []
        for tag in ("a", "b"):
            chat = client.post("/api/conversations",
                               json={"project_id": project["id"]}).json()
            plan = client.post(f"/api/conversations/{chat['id']}/task-plans",
                               json={"deterministic": True, "goal": "跑通一次最小实验"}).json()
            plan_pairs.append({"steps": plan["steps"], "seed": plan["seed"],
                               "title": plan["title"], "mode": plan["mode"],
                               "deterministic": plan["deterministic"]})
            golden["plan"][tag] = {
                "conversation_id": chat["id"], "seed": plan["seed"], "mode": plan["mode"],
                "title": plan["title"],
                "steps": [(s["id"], s["title"], s["status"]) for s in plan["steps"]],
            }
        record("确定性计划两次逐项相等",
               plan_pairs[0] == plan_pairs[1],
               f"{len(plan_pairs[0]['steps'])} 步 · seed={plan_pairs[0]['seed']} "
               f"· mode={plan_pairs[0]['mode']}")
        record("两次都是 deterministic 且顺序相同",
               plan_pairs[0]["deterministic"] is True
               and [s["id"] for s in plan_pairs[0]["steps"]]
               == [s["id"] for s in plan_pairs[1]["steps"]])

        # 7b 内核：同一份脚本跑两次，事件序列与 tool_calls 表逐项相等。
        #     ⚠️ 每次都换一个**新的**网关实例：`MockProvider._calls` 是实例计数器，
        #     复用它会让第二次从脚本中间起跳（见 `write_temp_config` 的说明）。
        original_gateway = app.state.kernel_loop.gateway
        try:
            for tag in ("a", "b"):
                chat = client.post("/api/conversations",
                                   json={"project_id": project["id"]}).json()
                app.state.kernel_loop.gateway = fresh_gateway(
                    app, task_script(sys.executable), response_text,
                )
                run_job = dispatch_chat_job(
                    app, project_id=project["id"], conversation_id=chat["id"],
                    agent_id="executor",
                )
                run_frames = collect_stream(client, run_job)
                status = await_job(client, run_job)["status"]
                # 危险操作的批准在两次运行里都会出现 —— 两边都批，序列才可比。
                # 审批单按 conversation_id 精确取：项目里还躺着上一段任务批过的那张。
                if status == "paused":
                    card = next(
                        c for c in client.get(
                            "/api/approvals", params={"project_id": project["id"]},
                        ).json()
                        if c["kind"] == "dangerous" and c["status"] == "pending"
                        and c["detail"].get("conversation_id") == chat["id"]
                    )
                    approved = client.post(f"/api/approvals/{card['id']}/approve",
                                           json={"note": "golden"})
                    resumed = approved.json().get("job_id")
                    if resumed:
                        run_frames = run_frames + collect_stream(client, int(resumed))
                        status = await_job(client, int(resumed))["status"]
                    else:
                        record(f"golden {tag}：批准后拿到恢复作业", False, approved.text[:120])
                record(f"golden {tag} 跑完", status == "succeeded", f"status={status}")
                session = app.state.session_factory()
                try:
                    from app.store.dao import tool_calls as golden_dao

                    called = [(r.tool_name, json.dumps(r.args, sort_keys=True), r.status)
                              for r in golden_dao.list_for_conversation(session, chat["id"])]
                finally:
                    session.close()
                golden["kernel"][tag] = {"conversation_id": chat["id"], "status": status,
                                         "events": normalize(run_frames), "tool_calls": called}
        finally:
            app.state.kernel_loop.gateway = original_gateway

        seq_a, seq_b = (golden["kernel"][t]["events"] for t in ("a", "b"))
        calls_a, calls_b = (golden["kernel"][t]["tool_calls"] for t in ("a", "b"))
        record("内核两次运行的事件序列逐项相等", seq_a == seq_b,
               f"{len(seq_a)} vs {len(seq_b)} 项")
        record("内核两次运行的 tool_calls 逐项相等", calls_a == calls_b,
               f"{[c[0] for c in calls_a]} vs {[c[0] for c in calls_b]}")
        archive["golden"] = golden

        step("8. 存档")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        archive["checks"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS]
        out_path.write_text(
            json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8",
        )
        record("事件序列已存档", out_path.exists(), str(out_path))
    finally:
        client.close()
        server.stop()
        if args.keep_tmp:
            print(f"\n（临时数据目录保留在 {workdir}）", flush=True)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n=== 结论 ===\n共 {len(CHECKS)} 项检查，通过 {len(CHECKS) - len(failed)}，"
          f"失败 {len(failed)}；存档 {out_path}")
    if failed:
        for name, _, detail in failed:
            print(f"  失败：{name} — {detail}")
        return 1
    print("内核走查通过：3 工具 × ≥5 步真实任务 / 全程流式 / deterministic golden 相等。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
