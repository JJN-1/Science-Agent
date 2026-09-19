#!/usr/bin/env python
"""US-311 纵向切片走查：自由文本 → S1 → 事件流 → 结果落库 → 成本与轨迹可见。

这条脚本走的是**真实 HTTP**（不是 TestClient），因为它要验证的正是
「用户敲下一句研究目标之后，整条链路里到底发生了什么」。

用法：

    # 1. 起后端（另开一个终端）
    cd backend && uv run uvicorn app.main:app --port 8000

    # 2. 走查
    cd backend && uv run python ../scripts/slice_walkthrough.py \\
        --goal "图神经网络在推荐系统中的可解释性"

要观察「长任务全程有进度」，把用户 config.yaml 里 mock 的 delay_ms 调大
（例如 1500），或在走查前设置 RESEARCHPILOT_DATA_DIR 指向一份带 delay_ms 的配置。

退出码 0 = 五条验收全过；非 0 = 有硬性断言失败（末尾会列出）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

CHECKS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  [{'ok' if ok else '!!'}] {name}{f' — {detail}' if detail else ''}", flush=True)


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="US-311 纵向切片走查")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    parser.add_argument("--title", default="纵向切片走查", help="项目标题")
    parser.add_argument(
        "--goal",
        default="图神经网络在推荐系统中的可解释性研究",
        help="自由文本入口的内容（会被当作研究目标）",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="整个走查的超时（秒）")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base, timeout=30.0)
    started = time.monotonic()

    step("0. 后端可达")
    health = client.get("/api/health").json()
    record("GET /api/health", health.get("status") == "ok", str(health))

    step("1. 建项目（自由文本入口的前置：目标是空的）")
    project = client.post(
        "/api/projects", json={"title": args.title, "goal": ""}
    ).json()
    project_id = project["id"]
    record("POST /api/projects", bool(project_id), f"project_id={project_id}")

    step("2. 自由文本 → 落成研究目标")
    updated = client.patch(
        f"/api/projects/{project_id}", json={"goal": args.goal}
    ).json()
    record("PATCH goal", updated["goal"] == args.goal, f"goal={updated['goal']!r}")

    step("3. 目标落库后触发 S1（受理即返回）")
    accept_started = time.perf_counter()
    accepted = client.post(f"/api/projects/{project_id}/stages/S1/run")
    accept_ms = (time.perf_counter() - accept_started) * 1000
    record("POST /stages/S1/run → 202", accepted.status_code == 202, accepted.text[:120])
    body = accepted.json()
    job_id = body["job_id"]
    record("受理耗时 < 100ms", accept_ms < 100, f"{accept_ms:.1f}ms")
    record("受理状态为 queued", body["status"] == "queued", body["status"])

    step("4. 订阅 SSE，逐帧记录进度")
    frames: list[dict] = []
    stamps: list[float] = []
    t0 = time.perf_counter()
    with client.stream(
        "GET", f"/api/jobs/{job_id}/stream", timeout=args.timeout
    ) as resp:
        record("流返回 text/event-stream",
               resp.headers.get("content-type", "").startswith("text/event-stream"),
               resp.headers.get("content-type", ""))
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            frame = json.loads(line[6:])
            frames.append(frame)
            stamps.append(time.perf_counter() - t0)
            label = frame["type"]
            extra = ""
            if label == "step":
                extra = f" ({frame['payload']['kind']}) {_short(frame['payload']['content'])}"
            elif label == "llm.call":
                p = frame["payload"]
                extra = f" {p['provider']}/{p['model']} ¥{p['cost']}"
            elif label == "stage.start":
                extra = f" {frame['payload']['stage_id']}"
            print(f"    +{stamps[-1] * 1000:6.0f}ms  #{frame['seq']:<3} {label}{extra}", flush=True)
    total_ms = (time.perf_counter() - t0) * 1000

    record("事件序列 seq 稠密", [f["seq"] for f in frames] == list(range(1, len(frames) + 1)))
    record("首帧为 job.queued", bool(frames) and frames[0]["type"] == "job.queued")
    record("末帧为终态事件",
           bool(frames) and frames[-1]["type"] in ("job.succeeded", "job.failed", "job.paused"),
           frames[-1]["type"] if frames else "无事件")
    record("流内有 step 事件（轨迹可见）", any(f["type"] == "step" for f in frames))
    record("流内有 llm.call 事件（成本可见）", any(f["type"] == "llm.call" for f in frames))
    if len(stamps) > 1:
        record("进度不是最后一刻一次性吐出",
               stamps[0] < total_ms * 0.9,
               f"首帧 +{stamps[0] * 1000:.0f}ms / 全程 {total_ms:.0f}ms")

    step("5. 作业终态与 run 落库")
    job = client.get(f"/api/jobs/{job_id}").json()
    record("作业 succeeded", job["status"] == "succeeded", f"status={job['status']} error={job['error']}")
    run_id = job["run_id"]
    record("作业指向 run", run_id is not None, f"run_id={run_id}")
    run = client.get(f"/api/runs/{run_id}").json()
    record("run 状态 succeeded", run["status"] == "succeeded", run["status"])
    kinds = [s["kind"] for s in run["steps"]]
    record("轨迹非空且含 llm_call", "llm_call" in kinds, str(kinds))

    step("6. 结果落库：黑板 + 决策 + 成本归因")
    board = client.get(f"/api/projects/{project_id}/blackboard").json()
    questions = [b for b in board if b["obj_type"] == "research_questions"]
    record("黑板有 research_questions", len(questions) == 1,
           f"{len(questions)} 条 / 全部 {[b['obj_type'] for b in board]}")
    record("候选问题非空",
           bool(questions) and bool(questions[0]["payload"].get("questions")),
           f"{len(questions[0]['payload'].get('questions', []))} 个候选" if questions else "")
    decisions = client.get(f"/api/projects/{project_id}/decisions").json()
    record("决策日志已记录", len(decisions) >= 1,
           " / ".join(d["decision"] for d in decisions)[:100])
    usage = client.get(f"/api/usage/summary?project_id={project_id}&dim=agent").json()
    record("成本归因到 agent", usage["rows"] and usage["rows"][0]["calls"] >= 1,
           f"rows={usage['rows']} total_cost={usage['total_cost']}")
    runs_cost = sum(
        float(s["content"].get("cost", 0)) for s in run["steps"] if s["kind"] == "llm_call"
    )
    record("run 内成本可见", runs_cost >= 0, f"¥{runs_cost:.6f}")

    step("7. 模拟刷新：重读同一份数据")
    runs_again = client.get(f"/api/projects/{project_id}/runs").json()
    board_again = client.get(f"/api/projects/{project_id}/blackboard").json()
    record("刷新后 run 仍在", len(runs_again) == 1 and runs_again[0]["id"] == run_id)
    record("刷新后黑板仍在", [b["id"] for b in board_again] == [b["id"] for b in board])
    events_again = client.get(f"/api/jobs/{job_id}/events").json()
    record("事件可回放（seq 一致）",
           [e["seq"] for e in events_again] == [f["seq"] for f in frames])

    step("8. 续传：after_seq 只补缺口")
    tail = client.get(f"/api/jobs/{job_id}/events?after_seq={frames[0]['seq']}").json() \
        if frames else []
    record("after_seq 续传正确",
           [e["seq"] for e in tail] == [f["seq"] for f in frames[1:]])

    elapsed = time.monotonic() - started
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n=== 结论 ===\n共 {len(CHECKS)} 项检查，通过 {len(CHECKS) - len(failed)}，"
          f"失败 {len(failed)}；耗时 {elapsed:.1f}s，job_id={job_id}，run_id={run_id}")
    if failed:
        for name, _, detail in failed:
            print(f"  失败：{name} — {detail}")
        return 1
    print("纵向切片走查通过：自由文本 → S1 → 流式进度 → 落库 → 刷新可见 → 成本与轨迹可见。")
    return 0


def _short(payload: dict) -> str:
    text = payload.get("text") or json.dumps(payload, ensure_ascii=False)
    text = " ".join(str(text).split())
    return text[:88] + ("…" if len(text) > 88 else "")


if __name__ == "__main__":
    sys.exit(main())
