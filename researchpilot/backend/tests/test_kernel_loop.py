"""内核循环（US-405）。

这份测试**不碰数据库**：``KernelLoop`` 只认识 ``KernelStore`` 协议与 ``LlmGateway``
的样子，所以这里用两个假件（``FakeStore`` / ``FakeGateway``）就能把循环的每一条路径
跑完，并逐条断言「写了什么、发了什么事件、往上游送了什么参数」。

这样做的理由是：循环是「模型意图」与「落库事实」的交界处。接了真实数据库之后，
断言会退化成「跑完没报错」，而真正要钉的是**中间那几步的形状** ——
``tool_choice`` 有没有强制、并行的结果是不是按调用序、失败计数是不是按
``(tool, args_hash)`` 累计、未注册的工具有没有真的**一次都没跑**。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.agent_kernel import context as kernel_context
from app.agent_kernel import loop as kernel_loop
from app.agent_kernel import planner
from app.agent_kernel.errors import LoopLimitError, StepFailureError
from app.agent_kernel.loop import KernelLoop, KernelRun
from app.agent_kernel.specs import AgentSpec
from app.agent_kernel.tools.base import (
    EXECUTE,
    READ,
    WRITE,
    Tool,
    ToolResult,
    ToolSpec,
)
from app.agent_kernel.tools.registry import ToolRegistry
from app.ai.base import ChatResponse, ToolCall
from app.ai.budget import BudgetExceeded
from app.jobs import events as job_events

# ── 假件 ────────────────────────────────────────

RUN = KernelRun(
    project_id=1, conversation_id=2, run_id=3,
    agent_id="kernel", stage_id="chat", tier="plan", job_id=4, goal="把这件事做完",
)


class FakeStore:
    """内存版 ``KernelStore``。记下每一次写入，供逐条断言。"""

    def __init__(self, history=None, plan=None) -> None:
        self.messages = list(history or [kernel_context.ContextMessage("user", "开工")])
        self._plan = plan
        self.events: list[tuple[str, dict]] = []
        self.records: list[kernel_loop.CallOutcome] = []
        self.plan_status: list[str] = []
        self.run_status: list[tuple[str, str | None]] = []
        self.saved_plans: list[planner.Plan] = []
        self.paused: tuple[BudgetExceeded, float] | None = None

    def history(self):
        return list(self.messages)

    def plan(self):
        return self._plan

    def append(self, role, content, *, tool_call_id=None, tool_calls=None):
        self.messages.append(kernel_context.ContextMessage(
            role, content, tool_call_id,
            tuple(ToolCall.from_dict(c) for c in tool_calls) if tool_calls else None,
        ))

    def record_tool_call(self, outcome):
        self.records.append(outcome)

    def save_plan(self, plan):
        self.saved_plans.append(plan)
        self._plan = plan

    def set_plan_status(self, status):
        self.plan_status.append(status)

    def set_run_status(self, status, error=None):
        self.run_status.append((status, error))

    def emit(self, event_type, payload):
        self.events.append((event_type, payload))

    def pause_for_budget(self, exc, *, suggested_grant):
        self.paused = (exc, suggested_grant)

    # 断言辅助
    def event_types(self) -> list[str]:
        return [t for t, _ in self.events]

    def payloads(self, event_type: str) -> list[dict]:
        return [p for t, p in self.events if t == event_type]


class FakeBudget:
    def suggested_grant(self, kind: str) -> float:
        return 5.0


class FakeGateway:
    """按脚本返回响应；记下调用参数供断言。

    脚本用完还继续被调用 = 循环没有按预期收敛，直接失败 —— 这比返回一个默认值
    更能暴露「多问了一轮」这类问题。
    """

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.budget = FakeBudget()

    def call(self, session, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("on_start") is not None:
            kwargs["on_start"](kwargs["tier"], [{"provider": "fake", "model": "m"}])
        if not self.responses:
            raise AssertionError("模型被多问了一轮（循环没有收敛）")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def reply(text: str = "", calls=()) -> ChatResponse:
    return ChatResponse(
        text=text, provider="fake", model="m",
        tool_calls=[ToolCall(id=c[0], name=c[1], arguments=c[2]) for c in calls] or None,
    )


class Tracker:
    """跨工具的并发峰值计数器。

    并行的唯一可信证据是「**同时**有几个工具在跑」。单个工具自己数自己只会恒等于 1
    （一个工具实例在一轮里只被调一次），所以计数器必须在工具之间共享；
    比对耗时更可靠 —— 耗时会受机器负载干扰，并发峰值是直接测出来的。
    """

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def exit(self) -> None:
        with self._lock:
            self.active -= 1


class ProbeTool(Tool):
    """记录调用次数、收到的参数，以及（共享计数器的）并发峰值。

    ``ok_sequence`` 用来模拟「同一次调用先失败后成功」这类序列 —— 自愈计数要靠它验证。
    """

    def __init__(
        self, name: str, permission: str = READ, *, ok: bool = True,
        ok_sequence=None, delay: float = 0.0, output=None, error: str = "",
        tracker: Tracker | None = None,
    ) -> None:
        self.spec = ToolSpec(
            name=name, description=f"{name} 探针",
            parameters={"type": "object", "properties": {}},
            permission=permission,
        )
        self.ok = ok
        self.ok_sequence = list(ok_sequence or [])
        self.delay = delay
        self.output = output if output is not None else {"tool": name}
        self.error = error
        self.calls: list[dict] = []
        self.tracker = tracker or Tracker()

    def run(self, args, ctx):
        self.tracker.enter()
        self.calls.append(args)
        try:
            if self.delay:
                time.sleep(self.delay)
            ok = self.ok_sequence.pop(0) if self.ok_sequence else self.ok
            return ToolResult(ok=ok, output=self.output, error="" if ok else self.error)
        finally:
            self.tracker.exit()


class _Borrowed:
    """借出去的 Session（并行只读路径用）。循环只调 rollback / close。"""

    def rollback(self) -> None:  # pragma: no cover - 只是被调用
        pass

    def close(self) -> None:  # pragma: no cover
        pass


def make_loop(*tools: Tool, spec: AgentSpec | None = None, **kwargs) -> KernelLoop:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    names = tuple(t.spec.name for t in tools)
    kwargs.setdefault("session_factory", _Borrowed)
    return KernelLoop(
        gateway=kwargs.pop("gateway"),
        tools=registry,
        spec=spec or AgentSpec(
            id="kernel", stage="chat", tier="plan", tools=names, max_steps=8,
        ),
        **kwargs,
    )


# ── 事件名的镜像必须对得上 ──────────────────────

def test_event_names_mirror_the_jobs_layer():
    """内核用字符串字面量发事件，就必须有人钉住它没写错。

    内核核心不 import ``app.jobs.events``（那个模块会连带把 ``store.dao`` 拉进来），
    所以用「镜像 + 对钉」的办法 —— 与 ``planner.VALID_MODES`` 镜像
    ``store.models.PLAN_MODES`` 同一条做法。拼错一个事件名，前端会静默漏渲染，
    而链路上没有任何一处会报错。
    """
    assert kernel_loop.EVENT_TOOL_CALL == job_events.TOOL_CALL
    assert kernel_loop.EVENT_TOOL_RESULT == job_events.TOOL_RESULT
    assert kernel_loop.EVENT_ASSISTANT_DELTA == job_events.ASSISTANT_DELTA
    assert kernel_loop.EVENT_PLAN_UPDATED == job_events.PLAN_UPDATED
    assert kernel_loop.EVENT_LLM_START == job_events.LLM_START
    assert kernel_loop.EVENT_STAGE_START == job_events.STAGE_START
    assert kernel_loop.EVENT_STAGE_SUCCEEDED == job_events.STAGE_SUCCEEDED
    assert kernel_loop.EVENT_STAGE_FAILED == job_events.STAGE_FAILED


def test_status_values_match_the_store_enum():
    """``rejected`` / ``failed`` 必须与 ``tool_calls`` 表的取值域一致。

    审计表用 ``ValueError`` 拦未知状态，所以不一致会表现为「记录突然写不进去」，
    而不是一条难看的记录。
    """
    from app.store.models import TOOL_CALL_STATUSES

    assert set(TOOL_CALL_STATUSES) == {"ok", "failed", "rejected"}
    assert kernel_loop.UNKNOWN_PERMISSION not in (
        READ, WRITE, EXECUTE,
    ), "未知权限是占位值，不该与任何真实等级重合"


# ── react 模式 ──────────────────────────────────

def test_react_runs_tools_until_the_model_stops_calling_them():
    """react 的终止条件 = 模型不再调工具。中间每一轮都真的执行了。"""
    echo = ProbeTool("echo")
    gateway = FakeGateway([
        reply(calls=[("c1", "echo", "{}")]),
        reply(calls=[("c2", "echo", '{"n": 2}')]),
        reply(text="做完了"),
    ])
    store = FakeStore()
    outcome = make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert outcome.status == "succeeded"
    assert outcome.mode == "react"
    assert outcome.rounds == 3
    assert outcome.tool_calls == 2
    assert [c for c in echo.calls] == [{}, {"n": 2}]
    # 模型停止调工具的那一轮之后**不再问** —— 多问一轮就是多花一次钱
    assert len(gateway.calls) == 3


def test_assistant_row_carries_tool_calls_and_tool_row_points_back():
    """回填的两条消息必须成对：assistant 带 ``tool_calls``、tool 带 ``tool_call_id``。

    少任何一半，下一轮送回模型的请求会被端点直接 400 拒收（协议要求一一对应）。
    """
    echo = ProbeTool("echo", output="结果正文")
    gateway = FakeGateway([reply(calls=[("c1", "echo", "{}")]), reply(text="好")])
    store = FakeStore()
    make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=store)

    assistant = [m for m in store.messages if m.role == "assistant"][0]
    tool = [m for m in store.messages if m.role == "tool"][0]
    assert [c.id for c in assistant.tool_calls] == ["c1"]
    assert tool.tool_call_id == "c1"
    assert tool.content == "结果正文"


def test_second_round_replays_the_tool_round_to_the_model():
    """第二轮实际送出去的 messages 里必须**同时**含调用与结果。

    这是「跨请求可回放」的循环内版本：只发结果不发调用，端点拒收；
    只发调用不发结果，模型不知道自己拿到了什么。
    """
    echo = ProbeTool("echo")
    gateway = FakeGateway([reply(calls=[("c1", "echo", "{}")]), reply(text="好")])
    make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=FakeStore())

    second = gateway.calls[1]["messages"]
    assert [m.role for m in second] == ["system", "system", "user", "assistant", "tool"]
    assistant = next(m for m in second if m.role == "assistant")
    assert [c.name for c in assistant.tool_calls] == ["echo"]


def test_react_ends_without_a_summary_when_the_model_says_nothing():
    """既没调工具也没说话 = 循环推不动，明确报错而不是无限重问。"""
    from app.agent_kernel.errors import KernelError

    gateway = FakeGateway([reply(text="  ")])
    with pytest.raises(KernelError) as exc:
        make_loop(ProbeTool("echo"), gateway=gateway).run(
            _Session(), run=RUN, store=FakeStore(),
        )
    assert exc.value.code == "AGENT-LOOP-002"


# ── plan_execute 模式 ───────────────────────────

def _plan(*steps, mode=planner.PLAN_EXECUTE, deterministic=False, seed=None):
    return planner.Plan(
        steps=tuple(steps), mode=mode, deterministic=deterministic, seed=seed,
        title="两步计划", plan_id=7,
    )


def test_plan_execute_advances_step_by_step_and_forces_the_step_tool():
    """计划里的 ``tool`` 是**硬约束**：``tool_choice`` 必须被强制到它身上。

    不强制的话，「计划写着调 A、实际调了 B」在界面上分不出来 ——
    而那正是 G2 第 8 条要防的「模型声称与系统执行混淆」。
    """
    one = ProbeTool("one")
    two = ProbeTool("two")
    gateway = FakeGateway([
        reply(calls=[("c1", "one", "{}")]),   # 第 1 步：调 called 工具
        reply(text="第一步完成"),               # 第 1 步收口
        reply(calls=[("c2", "two", "{}")]),   # 第 2 步
        reply(text="第二步完成"),
    ])
    store = FakeStore(plan=_plan(
        planner.PlanStep("s1", "第一步", tool="one"),
        planner.PlanStep("s2", "第二步", tool="two"),
    ))
    outcome = make_loop(one, two, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert outcome.mode == "plan_execute"
    assert outcome.steps_done == 2
    assert outcome.rounds == 4
    forced = [c["tool_choice"] for c in gateway.calls]
    assert forced == [
        {"type": "function", "function": {"name": "one"}},
        {"type": "function", "function": {"name": "one"}},
        {"type": "function", "function": {"name": "two"}},
        {"type": "function", "function": {"name": "two"}},
    ]
    # 步骤状态落库，且**只推进状态、不动内容**
    final = store.saved_plans[-1]
    assert [(s.id, s.status) for s in final.steps] == [("s1", "done"), ("s2", "done")]
    assert (final.title, final.mode) == ("两步计划", planner.PLAN_EXECUTE)
    assert store.plan_status == ["executing", "done"]


def test_plan_execute_does_not_force_a_tool_the_step_did_not_name():
    """步骤没指定工具 → ``auto``。计划不该替模型决定用什么。"""
    echo = ProbeTool("echo")
    gateway = FakeGateway([reply(text="这一步只出文本")])
    store = FakeStore(plan=_plan(planner.PlanStep("s1", "只写结论")))
    make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert gateway.calls[0]["tool_choice"] == "auto"
    assert store.saved_plans[-1].steps[0].status == "done"


def test_plan_whose_tool_is_not_available_falls_back_to_auto():
    """计划要的工具不在白名单里 → 降为 ``auto`` 并告警，**不是**把请求打挂。

    ``tool_choice`` 指向一个没下发的函数名，端点会直接 400 —— 那会把
    「计划与白名单不一致」这个配置问题，变成「这次对话整个发不出去」。
    """
    echo = ProbeTool("echo")
    gateway = FakeGateway([reply(text="降级后照常作答")])
    store = FakeStore(plan=_plan(planner.PlanStep("s1", "第一步", tool="不存在的工具")))
    make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert gateway.calls[0]["tool_choice"] == "auto"


def test_a_step_that_never_ran_its_tool_is_marked_skipped_not_done():
    """步骤声明了工具、模型却只回文本 → 记 ``skipped``，**不是** ``done``。

    记 ``done`` 会让计划卡片显示「已完成」，而实际什么都没发生。这是 D12
    「不把声称当执行」在内核内部的那一半：界面上分不出「工具真跑了」和
    「模型说自己跑了」，数据库就更不能替它混淆。
    """
    one = ProbeTool("one")
    gateway = FakeGateway([reply(text="我直接给结论，不调工具")])
    store = FakeStore(plan=_plan(planner.PlanStep("s1", "第一步", tool="one")))
    outcome = make_loop(one, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert one.calls == [], "工具压根不该被调用"
    assert outcome.steps_done == 0
    assert outcome.skipped_steps == 1
    assert store.saved_plans[-1].steps[0].status == "skipped"
    # 跳过必须**带原因**：不然用户只看到「跳过了」，不知道是模型的锅还是配置的锅
    notes = [n for p in store.payloads(kernel_loop.EVENT_PLAN_UPDATED)
             for n in p.get("notes", [])]
    assert any("s1" in n and "one" in n for n in notes), notes


def test_the_called_set_does_not_leak_from_one_step_to_the_next():
    """上一步调过的工具，不能替下一步「证明它跑过」。

    ``called`` 若不按步清零，第二步声明同一个工具、模型却没调时，就会被判成
    完成 —— 因为那个工具名在上一步出现过。这是把「工具名出现过」当成
    「这一步执行了」，两者之间的差别正是跳过检测的全部意义。
    """
    one = ProbeTool("one")
    gateway = FakeGateway([
        reply(calls=[("c1", "one", "{}")]),   # 第 1 步真的调了 one
        reply(text="第一步完成"),               # 第 1 步收口
        reply(text="第二步我也只说话"),          # 第 2 步：一个字都没调
    ])
    store = FakeStore(plan=_plan(
        planner.PlanStep("s1", "第一步", tool="one"),
        planner.PlanStep("s2", "第二步", tool="one"),
    ))
    outcome = make_loop(one, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert len(one.calls) == 1, "one 只在第一步被调用了一次"
    assert (outcome.steps_done, outcome.skipped_steps) == (1, 1)
    final = store.saved_plans[-1]
    assert [(s.id, s.status) for s in final.steps] == [("s1", "done"), ("s2", "skipped")]


def test_a_step_without_a_named_tool_is_not_penalised_for_talking():
    """没指定工具的步骤本来就该由模型决定做什么，只出文本仍是 ``done``。

    这条与上面两条成对：跳过检测只对「声明了工具」的步骤生效。把它扩大到所有
    步骤，会让纯推理步骤永远完不成，计划也就永远跑不完。
    """
    echo = ProbeTool("echo")
    gateway = FakeGateway([reply(text="这一步只需推理")])
    store = FakeStore(plan=_plan(planner.PlanStep("s1", "只推理")))
    outcome = make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert (outcome.steps_done, outcome.skipped_steps) == (1, 0)
    assert store.saved_plans[-1].steps[0].status == "done"


def test_deterministic_forces_temperature_zero_and_passes_seed():
    """D7：``deterministic`` = temperature 0 + 固定 seed + 禁并行。"""
    echo = ProbeTool("echo")
    gateway = FakeGateway([reply(text="确定性的答复")])
    store = FakeStore(plan=_plan(
        planner.PlanStep("s1", "第一步"), deterministic=True, seed=0,
    ))
    make_loop(echo, gateway=gateway, parallel=True).run(_Session(), run=RUN, store=store)

    call = gateway.calls[0]
    assert call["temperature"] == 0.0
    assert call["seed"] == 0


def test_non_deterministic_does_not_pin_temperature_or_seed():
    """普通路径必须保持原来的采样自由度：把 seed 当默认值传，用户会丢掉多样性。"""
    echo = ProbeTool("echo")
    gateway = FakeGateway([reply(text="答复")])
    make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=FakeStore())

    call = gateway.calls[0]
    assert call["temperature"] == kernel_loop.DEFAULT_TEMPERATURE
    assert call["seed"] is None


def test_react_is_used_when_there_is_no_executable_plan():
    """没有生效计划 = react，**不**凭空造一份 plan_execute。

    造出来的话，界面上会出现一份「没人批准过的计划」，而它看起来和用户批准过的一模一样。
    """
    echo = ProbeTool("echo")
    gateway = FakeGateway([reply(text="自由作答")])
    store = FakeStore(plan=None)
    outcome = make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert outcome.mode == "react"
    assert outcome.plan_id is None
    assert store.plan_status == []          # 没有计划就不该写计划状态
    assert kernel_loop.EVENT_PLAN_UPDATED not in store.event_types()


# ── 并行调用 ────────────────────────────────────

def test_read_only_calls_in_one_round_run_in_parallel():
    """只读工具并发：两路同时进入 ``run``（用共享计数器的峰值证，不看耗时）。"""
    tracker = Tracker()
    a = ProbeTool("a", READ, delay=0.05, tracker=tracker)
    b = ProbeTool("b", READ, delay=0.05, tracker=tracker)
    gateway = FakeGateway([
        reply(calls=[("c1", "a", "{}"), ("c2", "b", "{}")]),
        reply(text="好"),
    ])
    store = FakeStore()
    outcome = make_loop(a, b, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert tracker.max_active == 2
    assert [r.call.id for r in store.records] == ["c1", "c2"]
    assert outcome.tool_calls == 2


def test_write_calls_are_serialized():
    """写类工具**不并行**：共享 Session 并发会撞 SQLite 写锁，数据也可能互相打架。"""
    tracker = Tracker()
    a = ProbeTool("a", WRITE, delay=0.03, tracker=tracker)
    b = ProbeTool("b", WRITE, delay=0.03, tracker=tracker)
    gateway = FakeGateway([
        reply(calls=[("c1", "a", "{}"), ("c2", "b", "{}")]),
        reply(text="好"),
    ])
    make_loop(a, b, gateway=gateway).run(_Session(), run=RUN, store=FakeStore())

    assert tracker.max_active == 1


def test_deterministic_disables_parallel_even_for_read_only_calls():
    """D7：确定性模式下禁用并行 —— 复现是它的全部用途，并发引入的是调度噪声。"""
    tracker = Tracker()
    a = ProbeTool("a", READ, delay=0.03, tracker=tracker)
    b = ProbeTool("b", READ, delay=0.03, tracker=tracker)
    gateway = FakeGateway([
        reply(calls=[("c1", "a", "{}"), ("c2", "b", "{}")]),
        reply(text="完成"),
    ])
    store = FakeStore(plan=_plan(
        planner.PlanStep("s1", "第一步"), deterministic=True, seed=0,
    ))
    make_loop(a, b, gateway=gateway, parallel=True).run(_Session(), run=RUN, store=store)

    assert tracker.max_active == 1


def test_expand_failure_falls_back_to_serial(monkeypatch):
    """事件循环线程里没法起 ``anyio.run`` → 退回串行，而不是让这次对话失败。

    并行的收益只是省时间；为了它把整次对话打挂，是拿确定性换一个优化。
    """
    a = ProbeTool("a", READ)
    b = ProbeTool("b", READ)
    gateway = FakeGateway([
        reply(calls=[("c1", "a", "{}"), ("c2", "b", "{}")]),
        reply(text="好"),
    ])
    def explode(jobs):  # noqa: ANN001, ANN202
        raise RuntimeError("已经在一个事件循环里了")

    monkeypatch.setattr(kernel_loop, "_gather", explode)
    store = FakeStore()
    outcome = make_loop(a, b, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert outcome.tool_calls == 2
    assert len(a.calls) == 1 and len(b.calls) == 1


# ── 拒绝与自愈（D9）────────────────────────────

def test_unregistered_tool_is_reported_to_the_model_and_never_runs():
    """未注册的工具：模型看得见原因，而工具**一次都没跑**。

    这条是 US-404 的「两条失败通道」在循环里的落点：注册表面向代码调用方抛
    ``ToolError``，循环面向模型把它转成可见回执 —— 模型能做的只有换个工具。
    """
    echo = ProbeTool("echo")
    gateway = FakeGateway([
        reply(calls=[("c1", "不存在", "{}")]),
        reply(text="那就换个办法"),
    ])
    store = FakeStore()
    make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert len(store.records) == 1
    record = store.records[0]
    assert record.status == "rejected"
    assert record.permission == kernel_loop.UNKNOWN_PERMISSION
    assert echo.calls == []                       # 一次都没跑
    tool_row = [m for m in store.messages if m.role == "tool"][0]
    assert "未注册" in tool_row.content
    assert '"rejected": true' in tool_row.content


def test_whitelist_violation_is_rejected_without_running():
    """白名单外 → 拒绝（§5.3 最小权限）。拒的是「这次调用」，不是整个作业。"""
    echo = ProbeTool("echo")
    other = ProbeTool("other")
    gateway = FakeGateway([
        reply(calls=[("c1", "other", "{}")]),
        reply(text="知道了"),
    ])
    # 白名单只给 echo
    spec = AgentSpec(id="kernel", stage="chat", tier="plan", tools=("echo",), max_steps=8)
    store = FakeStore()
    outcome = make_loop(echo, other, gateway=gateway, spec=spec).run(
        _Session(), run=RUN, store=store,
    )

    assert other.calls == []
    assert outcome.rejected == 1
    assert "白名单" in store.records[0].error


def test_unparsable_arguments_are_not_silently_treated_as_empty():
    """参数解析失败**绝不**降级成空参数 —— 那会让工具带着默认行为跑出另一件事。"""
    echo = ProbeTool("echo")
    gateway = FakeGateway([
        reply(calls=[("c1", "echo", "{不是 JSON}")]),
        reply(text="重试后放弃"),
    ])
    store = FakeStore()
    make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert echo.calls == []
    assert store.records[0].status == "rejected"
    assert "LLM-TOOLS-001" in store.records[0].error


def test_three_consecutive_failures_on_the_same_args_stop_the_step():
    """D9：同一 ``(tool, args_hash)`` 连续三次 → 终止该步。**

    跳过它比终止它更贵：后续步骤会带着一个已知没做成的前提继续推理。
    """
    flaky = ProbeTool("flaky", ok=False, error="连接超时")
    gateway = FakeGateway([
        reply(calls=[("c1", "flaky", '{"x": 1}')]),
        reply(calls=[("c2", "flaky", '{"x": 1}')]),
        reply(calls=[("c3", "flaky", '{"x": 1}')]),
    ])
    store = FakeStore()
    with pytest.raises(StepFailureError) as exc:
        make_loop(flaky, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert len(flaky.calls) == 3
    assert "连续 3 次" in str(exc.value)
    assert exc.value.code == "AGENT-STEP-001"
    assert store.run_status[-1][0] == "failed"
    assert [r.status for r in store.records] == ["failed"] * 3
    # 参数哈希要出现在错误里，否则事后无从知道是哪一组参数绕不出来
    assert kernel_loop.args_hash({"x": 1}) in str(exc.value)


def test_changing_arguments_resets_the_failure_streak():
    """换参数重试是**正常操作**，不该被自愈计数误杀。

    这正是计数键必须带 ``args_hash`` 的理由：按工具名计数的话，模型换个参数
    也会被算进同一条命里，三次之后连正确的调用都不让试了。
    """
    flaky = ProbeTool("flaky", ok=False, error="路径不对")
    gateway = FakeGateway([
        reply(calls=[("c1", "flaky", '{"p": "a"}')]),
        reply(calls=[("c2", "flaky", '{"p": "b"}')]),
        reply(calls=[("c3", "flaky", '{"p": "c"}')]),
        reply(calls=[("c4", "flaky", '{"p": "d"}')]),
        reply(text="四组都不同，没有被当成连续失败"),
    ])
    store = FakeStore()
    outcome = make_loop(flaky, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert outcome.rounds == 5
    assert len(flaky.calls) == 4


def test_a_success_on_the_same_call_clears_the_streak():
    """D9 说的是**连续**失败：同一次调用成功一次，它的计数就该清零。

    ``(tool, args_hash)`` 的键决定了「清零」只发生在**同一个键**成功时 ——
    调另一个工具成功不算，因为那不是「这条路径走通了」。
    这个粒度正是「换参数重试」与「绕不出来」的分界。
    """
    flaky = ProbeTool(
        "flaky", ok_sequence=[False, False, True, False, False, False],
        error="偶发",
    )
    gateway = FakeGateway([
        reply(calls=[("c1", "flaky", '{"x": 1}')]),
        reply(calls=[("c2", "flaky", '{"x": 1}')]),
        reply(calls=[("c3", "flaky", '{"x": 1}')]),   # 成功 → 清零
        reply(calls=[("c4", "flaky", '{"x": 1}')]),
        reply(calls=[("c5", "flaky", '{"x": 1}')]),
        reply(calls=[("c6", "flaky", '{"x": 1}')]),   # 这才构成「连续 3 次」
    ])
    store = FakeStore()
    with pytest.raises(StepFailureError):
        make_loop(flaky, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert [r.status for r in store.records] == [
        "failed", "failed", "ok", "failed", "failed", "failed",
    ]


def test_a_different_tool_success_does_not_clear_the_streak():
    """调**别的**工具成功，不清零这条路径的计数。

    清零的条件是「同一个 ``(tool, args_hash)`` 成功了」。按「只要有一次成功就整体清零」
    实现的话，模型在两次失败之间插一个探针调用，就能无限重试同一个坏调用 ——
    而那正是 D9 要挡住的情形。
    """
    ok = ProbeTool("ok")
    flaky = ProbeTool("flaky", ok=False, error="偶发")
    gateway = FakeGateway([
        reply(calls=[("c1", "flaky", '{"x": 1}')]),
        reply(calls=[("c2", "ok", "{}")]),
        reply(calls=[("c3", "flaky", '{"x": 1}')]),
        reply(calls=[("c4", "ok", "{}")]),
        reply(calls=[("c5", "flaky", '{"x": 1}')]),
    ])
    store = FakeStore()
    with pytest.raises(StepFailureError):
        make_loop(ok, flaky, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert [r.status for r in store.records] == [
        "failed", "ok", "failed", "ok", "failed",
    ]


# ── 上限与熔断 ──────────────────────────────────

def test_step_limit_stops_a_model_that_never_converges():
    """步数上限是**必须**的：react 的终止条件由模型决定，模型不收敛就没有终点。"""
    echo = ProbeTool("echo")
    gateway = FakeGateway([
        reply(calls=[(f"c{i}", "echo", "{}")]) for i in range(5)
    ])
    store = FakeStore()
    with pytest.raises(LoopLimitError) as exc:
        make_loop(echo, gateway=gateway, max_steps=3).run(
            _Session(), run=RUN, store=store,
        )

    assert len(echo.calls) == 3, "跑到上限就该停，不能多跑一轮"
    assert exc.value.code == "AGENT-LOOP-001"
    assert "3" in str(exc.value)
    assert store.run_status[-1][0] == "failed"


def test_budget_exceeded_pauses_instead_of_failing():
    """预算熔断是**正常路径**：转 paused + 审批单，而不是 failed。

    复用 Sprint 3 已经跑通的「暂停 → 审批 → 恢复」语义（D4）——
    内核另起一套，用户会在同一个界面上看到两种形状的审批卡。
    """
    echo = ProbeTool("echo")
    gateway = FakeGateway([BudgetExceeded("agent_cost", {"message": "超了"})])
    store = FakeStore()
    outcome = make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert outcome.status == "paused"
    assert store.paused is not None
    assert store.paused[1] == 5.0                 # 建议豁免额度取自 BudgetManager
    assert store.run_status == []                 # 不是 failed：paused 由 store 自己写


def test_budget_pause_returns_the_plan_to_approved():
    """暂停后计划回到 ``approved`` 而不是留在 ``executing``。

    留在 ``executing`` 会让重规划接口被 ``has_running_plan`` 永远挡住 ——
    而暂停后没有任何 worker 在跑它，用户会失去唯一的出路。
    """
    echo = ProbeTool("echo")
    gateway = FakeGateway([BudgetExceeded("agent_steps", {"message": "超了"})])
    store = FakeStore(plan=_plan(planner.PlanStep("s1", "第一步")))
    make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert store.plan_status == ["executing", "approved"]


# ── 结果截断（D10）─────────────────────────────

def test_oversized_result_reaches_the_model_with_a_visible_marker():
    """截断发生在注册表（D10），循环要保证**带标记的那一份**回填给模型。

    静默截断等于对模型撒谎：它会在「结果就这么长」的前提下继续推理。
    """
    big = ProbeTool("big", output="x" * 5000)
    big.spec = ToolSpec(
        name="big", description="大输出", parameters={"type": "object", "properties": {}},
        result_max_bytes=256,
    )
    gateway = FakeGateway([reply(calls=[("c1", "big", "{}")]), reply(text="知道了")])
    store = FakeStore()
    make_loop(big, gateway=gateway).run(_Session(), run=RUN, store=store)

    row = [m for m in store.messages if m.role == "tool"][0]
    assert "已截断" in row.content
    assert len(row.content.encode("utf-8")) <= 256
    assert store.records[0].truncated is True


# ── 事件流（G2 第 2 条：全程可见）────────────────

def test_every_step_shows_up_in_the_event_stream():
    """事件流的顺序与内容：start → 计划 → llm.start → 助手文本 → 调用 → 结果 → 收尾。

    G2 第 2 条要求「全程流式可见」。前端只认事件，所以事件的**存在与先后**
    就是可见性的全部依据。
    """
    echo = ProbeTool("echo", EXECUTE)
    gateway = FakeGateway([reply(calls=[("c1", "echo", '{"a":1}')]), reply(text="收工")])
    store = FakeStore(plan=_plan(planner.PlanStep("s1", "第一步", tool="echo")))
    make_loop(echo, gateway=gateway).run(_Session(), run=RUN, store=store)

    types = store.event_types()
    assert types[0] == job_events.STAGE_START
    assert types[1] == job_events.PLAN_UPDATED
    assert types[-1] == job_events.STAGE_SUCCEEDED
    assert types.count(job_events.LLM_START) == 2
    assert types.count(job_events.TOOL_CALL) == 1
    assert types.count(job_events.TOOL_RESULT) == 1
    assert types.count(job_events.ASSISTANT_DELTA) == 1

    call_payload = store.payloads(job_events.TOOL_CALL)[0]
    assert call_payload["tool"] == "echo"
    assert call_payload["args"] == {"a": 1}
    assert call_payload["permission"] == EXECUTE      # 权限取自契约（D5），不是模型自称
    result_payload = store.payloads(job_events.TOOL_RESULT)[0]
    assert result_payload["ok"] is True
    assert "result_preview" in result_payload         # 事件只放预览，全文在审计表


def test_llm_start_reports_the_context_ledger():
    """``llm.start`` 要带上下文台账：等模型的时候用户唯一能看的就是这条。

    裁剪是静默的，不在这里摊开，「模型为什么忘了刚才说过的话」就永远查不出来。
    """
    echo = ProbeTool("echo")
    gateway = FakeGateway([reply(text="答复")])
    store = FakeStore(history=[
        kernel_context.ContextMessage("user", f"第{i}轮") for i in range(20)
    ])
    make_loop(echo, gateway=gateway, recent_turns=2).run(_Session(), run=RUN, store=store)

    payload = store.payloads(job_events.LLM_START)[0]
    assert payload["kept_turns"] == 2
    assert payload["collapsed_turns"] > 0
    assert any("摘要" in note for note in payload["context_notes"])
    assert payload["tools"] == ["echo"]
    assert payload["candidates"] == [{"provider": "fake", "model": "m"}]


def test_audit_records_stay_in_call_order():
    """审计表的顺序 = 调用顺序。G2 第 7 条要逐项比对两次运行，倒序会让比对没法写。"""
    a = ProbeTool("a", WRITE)
    b = ProbeTool("b", WRITE)
    gateway = FakeGateway([
        reply(calls=[("c1", "a", "{}"), ("c2", "b", "{}")]),
        reply(calls=[("c3", "b", "{}"), ("c4", "a", "{}")]),
        reply(text="好"),
    ])
    store = FakeStore()
    make_loop(a, b, gateway=gateway).run(_Session(), run=RUN, store=store)

    assert [r.call.name for r in store.records] == ["a", "b", "b", "a"]
    assert [r.call.id for r in store.records] == ["c1", "c2", "c3", "c4"]


# ── 无 job 的同步路径 ───────────────────────────

class _Session:
    """占位 Session：循环只把它转交给 gateway（假件不碰它）。"""
