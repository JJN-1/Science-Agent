#!/usr/bin/env python
"""真实模型的内核任务走查：让**模型自己**决定调什么工具（G2 第 1/2 条的真实模型版）。

与 `kernel_walkthrough.py` 的分工，说清楚免得重复劳动：

| | `kernel_walkthrough.py` | 本脚本 |
|---|---|---|
| 模型 | `mock` + `tool_script`（脚本驱动） | **真实模型**（自主决策） |
| 验证的命题 | 工具/权限/作业/事件链路的正确性与确定性 | 模型**能不能自己**把一句话拆成多步工具调用并落盘 |
| 断言口径 | 固定 5 步、产物内容逐字相等 | 不预设步数：≥1 次调用、产物真落盘、事件序列完整 |

两者都要，缺一不可：前者证明「手」是好的，后者证明「脑」会用手。

⚠️ **只在副本上跑**：把真实数据目录拷到临时目录（Key 在 Windows 凭据管理器，
不在数据目录里，所以副本读到的仍是同一份真实凭据），再关掉缓存 —— 缓存命中证明不了链路通。
真实库与真实用量因此不会被验证脚本污染（额度计数在 OpenRouter 侧，无法避免，这是真实调用的代价）。

⚠️ 用户消息用 ``messages_dao`` 直接写，不是 ``POST /messages``：那个接口会顺手派一个
**无特权**的 kernel 作业，而本脚本需要的是提权到 `executor` 的作业（沙箱工具只挂在它身上）。
两个作业跑同一个会话会让模型看到重复的用户消息。

用法：

    cd backend && uv run python ../scripts/kernel_real_task.py
    cd backend && uv run python ../scripts/kernel_real_task.py --timeout 900
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
REPO_ROOT = HERE.parents[2]
BACKEND_DIR = HERE.parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

TERMINAL_JOB_STATUSES = ("succeeded", "failed", "paused")

#: 给模型的自然语言任务：**不提示具体工具名**，让它自己决定用哪几个、用几步。
#: 明确写出「要真的做，不要只说」是因为不少模型会礼貌地描述一遍计划然后停下 ——
#: 那正是本脚本要揭露的失败模式，而不是要掩盖的。
TASK_PROMPT = (
    "请在项目工作目录里完成一次最小实验，要求真的动手做、不要只描述计划：\n"
    "1. 先看看当前工作目录里有什么；\n"
    "2. 写一个 Python 脚本 experiments/toy.py，它运行后会在 experiments/ 下写出一个 "
    "JSON 文件，内容含 ok=true 与 n=42；\n"
    "3. 运行这个脚本；\n"
    "4. 读回它写出的 JSON 并确认内容。\n"
    "做完后用一句话汇报结果。"
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


def prepare_copy(source: Path, target: Path) -> list[str]:
    """拷一份真实数据目录，只动两处配置。返回被补过 `tools` 能力的 provider 名。"""
    import yaml  # noqa: PLC0415

    shutil.copytree(source, target, dirs_exist_ok=True)
    cfg_path = target / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    ai = cfg.setdefault("ai", {})
    # 关缓存：要证明的是「真发一次请求也能通」，缓存命中证明不了
    ai["cache"] = {"enabled": False}
    patched: list[str] = []
    for name, body in (ai.get("providers") or {}).items():
        if not isinstance(body, dict) or body.get("type") == "mock":
            continue
        caps = list(body.get("capabilities") or [])
        # 少了 `tools`，内核的带工具请求会被能力门跳过（LLM-TOOLS-002），
        # 表现是「模型一个工具都没调」—— 那是配置问题，却长得像模型不行
        if "tools" not in caps:
            caps.append("tools")
            body["capabilities"] = caps
            patched.append(name)
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return patched


class Server:
    def __init__(self, app, port: int) -> None:
        import uvicorn  # noqa: PLC0415

        self._server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", access_log=False,
        ))
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


def await_job(client, job_id: int, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/jobs/{job_id}").json()
        if snapshot["status"] in TERMINAL_JOB_STATUSES:
            return snapshot
        time.sleep(0.3)
    return {**snapshot, "status": snapshot.get("status", "timeout")}


def collect_stream(client, job_id: int, *, timeout: float) -> list[dict]:
    frames: list[dict] = []
    t0 = time.perf_counter()
    with client.stream("GET", f"/api/jobs/{job_id}/stream", timeout=timeout) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            frame = json.loads(line[6:])
            frame["_at_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            frames.append(frame)
            print(f"    +{frame['_at_ms']:9.0f}ms  #{frame['seq']:<3} {frame['type']}"
                  f"{_brief(frame)}", flush=True)
    return frames


def _brief(frame: dict) -> str:
    payload = frame.get("payload") or {}
    kind = frame["type"]
    if kind == "tool.call":
        args = json.dumps(payload.get("args"), ensure_ascii=False, sort_keys=True)
        return f" {payload.get('tool')} {args[:110]}"
    if kind == "tool.result":
        return (f" {payload.get('tool')} ok={payload.get('ok')} "
                f"{payload.get('duration_ms')}ms {str(payload.get('error') or '')[:60]}")
    if kind == "assistant.delta":
        text = " ".join(str(payload.get("text") or "").split())
        return f" {text[:150]}{'…' if len(text) > 150 else ''}"
    if kind.startswith("job."):
        return f" status={payload.get('status')} reason={payload.get('reason') or '-'}"
    return ""


def main() -> int:  # noqa: C901 — 走查脚本按步骤直线铺开更好读
    parser = argparse.ArgumentParser(description="真实模型的内核任务走查")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--out", default=None)
    parser.add_argument("--timeout", type=float, default=600.0, help="单个作业的等待上限（秒）")
    parser.add_argument("--keep-tmp", action="store_true")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else REPO_ROOT / "tmp" / "kernel_real_task_events.json"
    source = Path(os.environ["APPDATA"]) / "ResearchPilot"
    workdir = Path(tempfile.mkdtemp(prefix="rp-real-task-"))
    patched = prepare_copy(source, workdir)
    print(f"真实数据目录副本：{workdir}")
    if patched:
        print(f"（给这些 provider 补上了 tools 能力：{patched} —— 缺它内核的带工具请求会被跳过）")

    os.environ["RESEARCHPILOT_DATA_DIR"] = str(workdir)

    import httpx  # noqa: PLC0415

    from app.agent_kernel.tokens import estimate_message_tokens  # noqa: PLC0415
    from app.main import create_app  # noqa: PLC0415

    app = create_app()
    server = Server(app, args.port or _free_port())
    server.start()
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{server.port}",
        timeout=httpx.Timeout(30.0, read=args.timeout),
    )
    archive: dict = {"task_prompt": TASK_PROMPT, "events": [], "jobs": [], "tool_calls": []}
    try:
        step("0. 后端就绪 + 当前路由（必须是真实 provider，否则这次留证没有意义）")
        print(f"  health={client.get('/api/health').json()}")
        routing = client.get("/api/settings/routing").json()
        target = routing["plan"][0]
        record("plan 档是真实 provider", target["provider"] != "mock",
               f"{target['provider']}/{target['model']}")
        providers = {p["id"]: p for p in client.get("/api/settings/providers").json()}
        pinned = providers.get(target["provider"], {})
        record("该 provider 有凭据且长度正常",
               bool(pinned.get("has_key")) and not pinned.get("key_suspicious"),
               f"has_key={pinned.get('has_key')} key_suspicious={pinned.get('key_suspicious')}")
        record("该 provider 声明了 tools 能力",
               "tools" in (pinned.get("capabilities") or []),
               f"{pinned.get('capabilities')}")

        step("1. 建项目与会话，写入一条用户消息")
        project = client.post("/api/projects", json={
            "title": "真实模型任务走查", "goal": "在沙箱里跑通一次最小实验",
        }).json()
        conversation = client.post("/api/conversations",
                                   json={"project_id": project["id"]}).json()
        session = app.state.session_factory()
        try:
            from app.store.dao import messages as messages_dao

            messages_dao.create(
                session, conversation_id=conversation["id"], role="user",
                content=TASK_PROMPT, tokens=estimate_message_tokens("user", TASK_PROMPT),
            )
            session.commit()
        finally:
            session.close()
        record("项目/会话/用户消息就绪", True,
               f"project={project['id']} conversation={conversation['id']}")

        step("2. 派作业并提权到 executor（沙箱工具只挂在它身上）")
        session = app.state.session_factory()
        try:
            job = app.state.job_runner.submit(
                session, project_id=project["id"], kind="chat",
                params={"conversation_id": conversation["id"], "agent_id": "executor"},
            )
            session.commit()
            job_id = int(job.id)
        finally:
            session.close()
        record("作业已受理", job_id > 0, f"job_id={job_id}")

        step("3. 订阅事件流，看模型自己决定调什么")
        frames = collect_stream(client, job_id, timeout=args.timeout)
        status = await_job(client, job_id, args.timeout)["status"]
        archive["jobs"].append({"job_id": job_id, "status": status,
                                "event_count": len(frames)})
        # ⚠️ 序号只在**单个作业内**稠密：批准后的恢复是**另一个作业**，seq 从 1 重新开始。
        # 因此「稠密」必须分段判，而段必须是**拷贝** —— `frames += more` 是在原列表上
        # 原地扩展，`segments[0]` 会跟着一起变长（第一版就栽在这个引用语义上：
        # 两段拼成一个 30 帧的列表，于是判「seq 不稠密」，而真正的原因是断言看错了对象）。
        segments = [list(frames)]

        # 危险操作（run_command）会整轮挂起等人批准 —— 真实链路的一部分，照批。
        # ⚠️ **要循环**：`dangerous` 每次必批、不留记忆（D5），所以模型第二次调它还会再挂起
        # （实测确实如此）。只处理一次审批，任务就会停在第二次挂起处 ——
        # 那看起来像「模型没做完」，其实是脚本没把该批的批掉。上限 5 轮：
        # 再多说明模型在绕圈，那不是本脚本该替它兜的事。
        approvals: list[dict] = []
        for round_index in range(1, 6):
            if status != "paused":
                break
            step(f"4.{round_index} 危险操作挂起 → 走真实审批接口放行 → 继续")
            reason = (frames[-1].get("payload") or {}).get("reason") if frames else None
            record(f"第 {round_index} 次暂停原因是等待批准", reason == "dangerous", str(reason))
            cards = client.get("/api/approvals",
                               params={"project_id": project["id"]}).json()
            pending = [c for c in cards if c["kind"] == "dangerous"
                       and c["status"] == "pending"
                       and c["detail"].get("conversation_id") == conversation["id"]]
            if not pending:
                record(f"第 {round_index} 次有待批单", False, f"{[c['kind'] for c in cards]}")
                break
            tools = [c["tool"] for c in pending[0]["detail"]["pending"]]
            record(f"第 {round_index} 次待批的正是那条命令", tools == ["run_command"], str(tools))
            approvals.append({"round": round_index, "approval_id": pending[0]["id"], "tools": tools})

            approved = client.post(f"/api/approvals/{pending[0]['id']}/approve",
                                   json={"note": "走查批准"})
            resumed = approved.json().get("job_id")
            record(f"第 {round_index} 次批准后派出了恢复作业", bool(resumed), f"job_id={resumed}")
            if not resumed:
                break
            more = collect_stream(client, int(resumed), timeout=args.timeout)
            frames = frames + more          # 新建列表：别让旧段跟着变长
            segments.append(list(more))
            status = await_job(client, int(resumed), args.timeout)["status"]
            archive["jobs"].append({"job_id": resumed, "status": status,
                                    "event_count": len(more)})
        archive["approvals"] = approvals
        if len(approvals) > 1:
            print(f"  （模型一共触发了 {len(approvals)} 次危险操作审批 —— "
                  f"`dangerous` 每次必批、不留记忆，这正是设计要的行为）")

        archive["events"] = frames
        record("任务跑完", status == "succeeded", f"status={status}")

        step("5. 模型的工具使用情况（这是本脚本的真正结论）")
        calls = [f for f in frames if f["type"] == "tool.call"]
        results = [f for f in frames if f["type"] == "tool.result"]
        used = [f["payload"].get("tool") for f in calls]
        archive["tool_calls"] = [
            {"tool": f["payload"].get("tool"), "args": f["payload"].get("args"),
             "permission": f["payload"].get("permission")} for f in calls
        ]
        print(f"  模型自主调用 {len(calls)} 次：{used}")
        record("模型真的动了工具（≥1 次调用）", len(calls) >= 1, f"{len(calls)} 次")
        record("每次调用都有结果", len(results) == len(calls), f"{len(results)} 结果 / {len(calls)} 调用")
        record("模型用了 ≥2 种不同工具", len(set(used)) >= 2, f"{sorted(set(used))}")
        record("模型动手写了文件", "write_file" in used, str(used))
        record("模型真的执行了命令", "run_command" in used, str(used))
        failed = [f["payload"].get("tool") for f in results if not f["payload"].get("ok")]
        record("没有失败的调用", not failed, str(failed))

        step("6. 沙箱里真的有产物")
        sandbox = workdir / "workspace" / f"project-{project['id']}"
        toy = sandbox / "experiments" / "toy.py"
        record("模型写的脚本存在", toy.exists(), str(toy))
        # ⚠️ 不硬编码 JSON 文件名：任务提示没规定它叫什么，模型自己命名（实测叫过
        # `result.json`，也叫过 `toy_result.json`）。钉文件名等于把「模型有自主权」
        # 这件事从断言里删掉，然后因为模型「不听话」而报红。
        candidates = sorted((sandbox / "experiments").glob("*.json")) \
            if (sandbox / "experiments").exists() else []
        record("脚本跑出的 JSON 存在", bool(candidates), str([p.name for p in candidates]))
        if candidates:
            payload = json.loads(candidates[0].read_text(encoding="utf-8"))
            record("JSON 内容符合要求", payload.get("ok") is True and payload.get("n") == 42,
                   json.dumps(payload, ensure_ascii=False)[:160])
        archive["artifacts"] = {
            path.relative_to(sandbox).as_posix(): path.read_text(encoding="utf-8", errors="replace")[:2000]
            for path in sorted(sandbox.rglob("*")) if path.is_file()
        }

        step("7. 流式与落库")
        for index, seg in enumerate(segments, start=1):
            seqs = [f["seq"] for f in seg]
            record(f"第 {index} 段事件序号稠密（1..N 无缺口）",
                   seqs == list(range(1, len(seqs) + 1)), f"{len(seqs)} 帧")
            record(f"第 {index} 段进度不是最后一次性吐出",
                   seg[0]["_at_ms"] < max(seg[-1]["_at_ms"] * 0.5, 1.0),
                   f"首帧 +{seg[0]['_at_ms']}ms / 终帧 +{seg[-1]['_at_ms']}ms")
            record(f"第 {index} 段末帧是终态事件",
                   seg[-1]["type"] in ("job.succeeded", "job.failed", "job.paused"),
                   seg[-1]["type"])
        # 模型不调 run_command 时不会有审批，也就只有一段 —— 两种情况都算收口
        record("任务收口（有审批时两段拼成一条）", status == "succeeded" and len(segments) <= 2,
               f"{len(segments)} 段 / 共 {len(frames)} 帧 / 终态 {status}")
        text = "".join(str((f.get("payload") or {}).get("text") or "")
                       for f in frames if f["type"] == "assistant.delta")
        print(f"  模型最终汇报：{text[:400]}")
        archive["final_text"] = text
        session = app.state.session_factory()
        try:
            from app.store.dao import tool_calls as tool_calls_dao

            rows = tool_calls_dao.list_for_conversation(session, conversation["id"])
            print("  落库的工具调用：",
                  [(r.tool_name, r.status, r.permission) for r in rows])
            record("工具调用已落库且与事件流一致", len(rows) == len(calls),
                   f"库 {len(rows)} / 流 {len(calls)}")
        finally:
            session.close()

        step("8. 存档")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        archive["checks"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS]
        out_path.write_text(json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  存档：{out_path}")
    finally:
        client.close()
        server.stop()
        if args.keep_tmp:
            print(f"（副本保留在 {workdir}）")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n=== 结论 ===\n共 {len(CHECKS)} 项检查，通过 {len(CHECKS) - len(failed)}，失败 {len(failed)}")
    if failed:
        for name, _, detail in failed:
            print(f"  失败：{name} — {detail}")
        return 1
    print("真实模型任务走查通过：模型自主完成了多步工具任务，产物落在沙箱里。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
