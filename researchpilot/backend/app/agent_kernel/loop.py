"""内核循环（US-405）—— 把「模型的一次选择」变成「系统的一次动作」。

单轮的形状是固定的四步：**装配上下文 → 要一次决策 → 执行工具 → 结果回填**，
重复到终止条件成立。整个文件只做这一件事，但有三处判断必须写在明面上。

## 一、双模式的差别只有两处

| | 驱动源（放进提示词的「当前该做什么」） | 终止条件 |
|---|---|---|
| ``plan_execute`` | 计划里第一个未完成的步骤（含它指定的工具） | 计划跑完 |
| ``react`` | 研究目标本身（无固定步骤） | 模型停止调用工具 |

其余全部共用：同一套装配、同一套工具执行、同一套自愈与事件。分成两个类会让
「并行调用」「结果截断」「错误自愈」各写两遍，而它们与模式无关。

**``plan_execute`` 里步骤指定的 ``tool`` 是硬约束而不是建议**：这一轮下发给模型的
``tools`` 仍按白名单过滤，而 ``tool_choice`` 会强制到该步骤指定的那一个。计划说了做什么、
系统就只让做什么 —— 否则「计划写着调 A、实际调了 B」正是 G2 第 8 条要防的那种混淆。

**没有生效计划 = ``react``，不降级成 ``plan_execute``**：计划是用户批准过的东西，
内核凭空造一份「计划」会让界面上出现一份没人批过的步骤表（与 ``AGENT-PLAN-002``
同一条原则：标注与行为必须一致）。

## 二、``(tool, args_hash)`` 三次自愈（D9）

计数键带上参数哈希，是因为**「换参数重试」是模型该做的正常操作**，按工具名计数会把它
误杀。反过来，同一份输入连着失败三次说明模型陷入了循环 —— 它在用同样的输入期待
不同的输出。此时终止该步而不是跳过：跳过等于让后面所有步骤都建立在一个已知没做成的
前提上继续推理。

**契约违规也走这条通道**（``ToolError``：未注册 / 不在白名单 / 参数不合 schema）。
这里与注册表的处置**刻意相反**，因为调用方不同：注册表面向代码调用方，白名单写错
不该让它去改；而循环面向的是模型 —— 模型确实能「换个工具」或「换组参数」，
它需要的是一条能看见的错误信息，而不是一个炸掉整个作业的异常。审计表里两者仍然分开
（``rejected`` 与 ``failed``），因为对**事后**的人而言「压根不该试」和「试了没成」
是完全不同的两件事。

## 三、并行只在「同轮多个只读调用」时启用

判据用 ``permission == READ`` 而不是新加一个字段：只读工具并发与串行对系统状态
**没有差异**，唯一差别是耗时，因此「结果按调用序回填」是安全的。写类工具共享
``Session`` 并发会直接撞 SQLite 写锁（而且 ``Session`` 本身不是线程安全的），
所以它们一律串行。D7 还要求 ``deterministic`` 下禁用并行 —— 确定性的全部用途就是复现，
并发引入的是调度噪声。

并行时每个调用**各自借一个 Session**：只读工具不改数据，独立 Session 没有副作用，
而共享同一个 Session 跨线程使用是未定义行为。
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import anyio

from app.agent_kernel import context as kernel_context
from app.agent_kernel.errors import KernelError, LoopLimitError, StepFailureError, ToolError
from app.agent_kernel.permissions import (
    ApprovalRequired,
    PendingCall,
    authorize,
    scope_of,
)
from app.agent_kernel.planner import PLAN_EXECUTE, REACT, Plan, PlanStep
from app.agent_kernel.specs import CONVERSATION_SPEC, AgentSpec
from app.agent_kernel.tools.base import READ, ToolContext, ToolResult
from app.agent_kernel.tools.registry import ToolRegistry
from app.ai.base import ChatMessage, ChatResponse, ToolArgumentsError, ToolCall
from app.ai.budget import BudgetExceeded
from app.observability.logging import get_logger

logger = get_logger("agent_kernel.loop")

#: 循环轮数上限。轮 = 一次模型调用；``react`` 的终止条件是「模型不再调工具」，
#: 模型若不收敛，没有这个上限就是一个不会返回的循环，且每一轮都真花钱。
DEFAULT_MAX_STEPS = 20
#: 同一 ``(tool, args_hash)`` 连续失败多少次终止该步（D9）
SELF_HEAL_LIMIT = 3
#: 默认温度；确定性模式覆盖为 0.0（D7）
DEFAULT_TEMPERATURE = 0.7
#: 未注册的工具在审计表里的权限占位：连工具都不存在，谈不上权限等级
UNKNOWN_PERMISSION = "unknown"

# ── 事件名镜像（与 ``app.jobs.events`` 一致，有单测对钉）────
# 内核核心不 import ``app.jobs.events``：那个模块会连带 import ``store.dao.jobs``，
# 而「内核不反向依赖 store」是既定分层。与 ``planner.VALID_MODES`` 镜像
# ``store.models.PLAN_MODES`` 是同一种做法：镜像 + 单测钉住，而不是把依赖反向拉过来。
EVENT_TOOL_CALL = "tool.call"
EVENT_TOOL_RESULT = "tool.result"
EVENT_ASSISTANT_DELTA = "assistant.delta"
EVENT_PLAN_UPDATED = "plan.updated"
EVENT_LLM_START = "llm.start"
EVENT_STAGE_START = "stage.start"
EVENT_STAGE_SUCCEEDED = "stage.succeeded"
EVENT_STAGE_FAILED = "stage.failed"
# 危险操作待批准（US-406）。界面上它**必须**与「模型在思考」区分开：
# 这条事件出来之后内核就停住了，在等人；没有它，用户看到的是「卡住不动」。
EVENT_APPROVAL_REQUIRED = "approval.required"

#: 未注册 / 不在白名单 / 参数不合 schema 时给模型的回执提示。
#: 这三个都是**模型能自己修的问题**（换个工具或换组参数），所以把话说全。
_REJECTED_HINT = "该调用没有执行；请改用其它工具，或修正参数后重试。"

_ROLE_PROMPT = (
    "你是 ResearchPilot 的研究内核，正在一个科研项目的会话里推进任务。\n"
    "工作方式：\n"
    "1. 需要外部信息或需要执行动作时，调用本次下发的工具；**不要凭记忆编造工具结果**。\n"
    "2. 一次可以发起多个互不依赖的调用；有先后依赖时，先等前一个的结果再决定下一步。\n"
    "3. 工具结果会以工具消息回传。失败信息说明了原因，据此换参数或换工具重试。\n"
    "4. 事情做完、或需要人来拍板时，**停止调用工具**，用简洁的中文说明现状与建议。\n"
    "5. 不确定就说不确定；不得虚构数据、文献、引用或结论。"
)


# ── 数据形状 ────────────────────────────────────

@dataclass(frozen=True)
class KernelRun:
    """一次内核循环的执行坐标。

    这些都是**内核之外**的定位信息（属于哪个项目、哪个会话、哪次 run），由装配方
    （作业层）给出。工具拿到的 ``project_id`` 也来自这里而不是模型参数 ——
    模型若能指定 ``project_id``，它就等于能跨项目读写。
    """

    project_id: int
    conversation_id: int
    run_id: int
    agent_id: str
    stage_id: str
    tier: str
    job_id: int | None = None
    goal: str = ""


@dataclass
class LoopOutcome:
    """循环的结算。``status`` 与作业终态同名（succeeded / failed / paused）。"""

    status: str = "succeeded"
    mode: str = PLAN_EXECUTE
    rounds: int = 0
    steps_done: int = 0
    skipped_steps: int = 0
    tool_calls: int = 0
    rejected: int = 0
    plan_id: int | None = None
    reason: str = ""


@dataclass
class CallOutcome:
    """一次工具调用的完整结算，供落库、回填与自愈计数共用一份数据。

    ``status`` 三态与 ``store.models.TOOL_CALL_STATUSES`` 一致：
    ``ok`` 跑成了 / ``failed`` 跑了但失败 / ``rejected`` 压根没跑（调用方违规）。
    """

    call: ToolCall
    args: dict = field(default_factory=dict)
    permission: str = READ
    status: str = "ok"
    output: Any = None
    error: str = ""
    duration_ms: int = 0
    truncated: bool = False
    #: 本次调用是「人工批准之后」才执行的（US-406）。落进 ``tool_calls.approval_id``，
    #: 让「这条命令是谁批的」在审计表里有一条可查的边 —— 只记在事件流里的话，
    #: 事后要回答这个问题就得把事件翻一遍再在内存里关联。
    approval_id: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        """自愈计数键：``(tool, args_hash)``（D9）。"""
        return (self.call.name, args_hash(self.args))

    @property
    def content(self) -> str:
        """回填给模型的那段文本。

        成功时直接给工具自己的输出（字符串原样、其余序列化成 JSON）—— 多包一层信封
        只会让模型多读一层，而且它得先学会这个信封才知道怎么用结果。失败与拒绝才给信封，
        因为此时「这是什么」比「值是什么」更重要。
        """
        if self.status == "ok":
            if isinstance(self.output, str):
                return self.output
            return json.dumps(self.output, ensure_ascii=False, sort_keys=True, default=str)
        envelope: dict[str, Any] = {"ok": False, "error": self.error}
        if self.status == "rejected":
            envelope["rejected"] = True
            envelope["hint"] = _REJECTED_HINT
        if self.truncated:
            envelope["truncated"] = True
        return json.dumps(envelope, ensure_ascii=False, sort_keys=True)


def args_hash(args: dict[str, Any]) -> str:
    """参数的规范化短哈希。

    ``sort_keys`` 是必须的：同一组参数的键序由模型决定（而且不同轮次可能不同），
    不排序会让「同样的调用」每次算出不同的哈希，自愈计数就永远不会累积 ——
    三次失败被记成三次各不相同的失败，于是循环永不终止。
    """
    raw = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class KernelStore(Protocol):
    """循环需要的外部世界。**刻意做成窄接口**：内核核心不 import ``app.store``。

    这不是为了抽象而抽象。循环是「模型意图」与「落库事实」的交界处，它需要写库、
    发事件、读历史；把它和某个具体的 ORM 绑死，会让「这一步到底写了什么」在单测里
    无法脱库验证 —— 而这类写入恰恰是最需要被逐条断言的部分。

    实现见 ``app/orchestration/kernel_store.py``：唯一同时认识 DAO 与内核的一方。
    """

    def history(self) -> list[kernel_context.ContextMessage]:
        """当前会话的全部消息，时间正序。"""

    def plan(self) -> Plan | None:
        """生效中的计划（``approved`` / ``executing``），没有则 ``None``。"""

    def append(
        self, role: str, content: str, *,
        tool_call_id: str | None = None,
        tool_calls: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        """追加一条消息（assistant / tool 由内核写，用户消息走 API）。"""

    def record_tool_call(self, outcome: CallOutcome) -> None:
        """落一条 ``tool_calls`` 审计记录。"""

    def save_plan(self, plan: Plan) -> None:
        """把计划（含各步最新状态）写回。"""

    def set_plan_status(self, status: str) -> None:
        """改计划状态。没有生效计划时是空操作。"""

    def set_run_status(self, status: str, error: str | None = None) -> None:
        """把 ``agent_runs`` 那一行推到终态。"""

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """写一条作业事件（立即提交，SSE 读端才看得见）。"""

    def approval_grants(self) -> Collection[str]:
        """已生效的审批记忆键（``app_config`` 里的 ``tool_grant:*``，D5）。

        由 store 提供而不是让闸门去查库：``authorize`` 保持纯函数，
        判定就能脱库逐档断言 —— 而「哪些动作需要批准」恰恰是最该被钉死、
        又最不该依赖数据库状态的东西。
        """

    def pause_for_approval(self, exc: ApprovalRequired) -> None:
        """需要人工批准 → 暂停 + 审批单（D4：复用 approvals 表，``kind=dangerous``）。"""

    def pause_for_budget(self, exc: BudgetExceeded, *, suggested_grant: float) -> None:
        """预算熔断 → 暂停 + 审批单（复用既有语义，见 D4）。"""


# ── 循环 ────────────────────────────────────────

class KernelLoop:
    """内核循环。**无状态**：每次 ``run`` 的进度只存在于计划与消息里。"""

    def __init__(
        self,
        *,
        gateway: Any,
        tools: ToolRegistry,
        spec: AgentSpec | None = None,
        max_steps: int | None = None,
        parallel: bool = True,
        session_factory: Any = None,
        budget_tokens: int = kernel_context.DEFAULT_BUDGET_TOKENS,
        recent_turns: int = kernel_context.DEFAULT_RECENT_TURNS,
        max_tokens: int = 1024,
    ) -> None:
        self.gateway = gateway
        self.tools = tools
        self.spec = spec or CONVERSATION_SPEC
        #: ``None`` = 用 ``AgentSpec.max_steps``（§5.3 的 per-agent 覆盖）
        self.max_steps = max_steps
        self.parallel = parallel
        #: 并行执行只读工具时借独立 Session；为 ``None`` 时退化为串行
        self.session_factory = session_factory
        self.budget_tokens = budget_tokens
        self.recent_turns = recent_turns
        self.max_tokens = max_tokens

    # ── 入口 ────────────────────────────────────

    def run(
        self,
        session: Any,
        *,
        run: KernelRun,
        store: KernelStore,
        plan: Plan | None = None,
        spec: AgentSpec | None = None,
    ) -> LoopOutcome:
        """跑一次循环，把 ``agent_runs`` 那一行推到终态后返回。

        **预算熔断在本地转成 ``paused``**：它是一条正常路径而不是失败 ——
        复用 Sprint 3 已跑通的「暂停 → 审批 → 恢复」语义（D4）。其余内核错误
        原样上抛，作业层的兜底会落成 ``job.failed``，而 ``KernelError`` 的码就写在
        ``str(exc)`` 最前面（``errors.py`` 的约定），一路带到事件里。
        """
        spec = spec or self.spec
        plan = plan if plan is not None else store.plan()
        mutable = _MutablePlan(plan) if (plan is not None and plan.steps) else None

        if mutable is not None:
            mode, deterministic = mutable.plan.mode, mutable.plan.deterministic
        else:
            mode, deterministic = REACT, False

        if mode not in (PLAN_EXECUTE, REACT):
            raise KernelError(f"未知编排模式：{mode}", code="AGENT-PLAN-001")
        if deterministic and mode != PLAN_EXECUTE:
            raise KernelError(
                f"确定性模式不接受 {mode}（D7：deterministic 恒为 plan_execute）",
                code="AGENT-PLAN-002",
            )

        outcome = LoopOutcome(
            mode=mode, plan_id=mutable.plan.plan_id if mutable else None,
        )
        store.emit(EVENT_STAGE_START, {
            "run_id": run.run_id, "agent_id": run.agent_id, "stage_id": run.stage_id,
            "conversation_id": run.conversation_id, "mode": mode,
            "deterministic": deterministic, "plan_id": outcome.plan_id,
        })
        if mutable is not None:
            store.set_plan_status("executing")
            store.emit(EVENT_PLAN_UPDATED, {
                "plan_id": outcome.plan_id, "mode": mode,
                "deterministic": deterministic, "status": "executing",
                "steps": mutable.plan.to_steps_payload(),
            })

        try:
            outcome = self._loop(
                session, run=run, store=store, mutable=mutable, spec=spec,
                mode=mode, deterministic=deterministic,
                max_steps=self.max_steps or spec.max_steps,
                strikes={}, outcome=outcome,
            )
        except BudgetExceeded as exc:
            # 计划回到 approved 而不是 failed：它确实还没跑完，而且暂停后没有任何
            # worker 在跑它 —— 留在 executing 会让重规划接口被 ``has_running_plan``
            # 永远挡住，用户失去唯一的出路。恢复走第 7 步的检查点续跑。
            if mutable is not None:
                store.save_plan(mutable.plan)
                store.set_plan_status("approved")
            store.pause_for_budget(exc, suggested_grant=_suggested_grant(self.gateway, exc))
            outcome.status, outcome.reason = "paused", str(exc)
            return outcome
        except ApprovalRequired as exc:
            # 与预算熔断同一档：**它不是失败**。人还没看到审批单（甚至还没机会看），
            # 落成 failed 会让「等待批准」在界面上表现为「跑挂了」，
            # 而用户的第一反应是重试 —— 于是又开一张审批单。
            #
            # ⚠️ 这个分支必须排在 ``except Exception`` 前面。``ApprovalRequired``
            # 刻意**不继承** ``KernelError``（见 permissions.py），就是不让下面那条
            # 「一切内核错误都是失败」的兜底把它顺手吞掉；但它是 ``Exception``，
            # 顺序写反了照样会被兜住。
            if mutable is not None:
                store.save_plan(mutable.plan)
                store.set_plan_status("approved")
            store.pause_for_approval(exc)
            outcome.status, outcome.reason = "paused", exc.reason
            return outcome
        except Exception as exc:  # noqa: BLE001 —— 现场先落库，再让异常继续往上走
            if mutable is not None:
                store.save_plan(mutable.plan)
                store.set_plan_status("failed")
                store.emit(EVENT_PLAN_UPDATED, {
                    "plan_id": outcome.plan_id, "mode": mode, "status": "failed",
                    "error": str(exc), "steps": mutable.plan.to_steps_payload(),
                })
            store.set_run_status("failed", str(exc))
            store.emit(EVENT_STAGE_FAILED, {
                "run_id": run.run_id, "error": str(exc),
                "code": getattr(exc, "code", ""),
                "rounds": outcome.rounds, "tool_calls": outcome.tool_calls,
            })
            raise

        # ⚠️ 顺序是刻意的：**先写计划收尾，再宣布这次运行结束**。
        # 反过来（终态事件之后还有事件）会让按「收到终态即停止消费」实现的读端漏掉
        # 计划那一条 —— 而它恰恰是界面刷新步骤状态的依据，表现为「跑完了但卡片还停在
        # 第一步」。终态事件必须真的是最后一条。
        if mutable is not None:
            store.save_plan(mutable.plan)
            store.set_plan_status("done")
            store.emit(EVENT_PLAN_UPDATED, {
                "plan_id": outcome.plan_id, "mode": mode, "status": "done",
                "steps": mutable.plan.to_steps_payload(),
            })
        store.set_run_status("succeeded")
        store.emit(EVENT_STAGE_SUCCEEDED, {
            "run_id": run.run_id, "mode": mode, "rounds": outcome.rounds,
            "tool_calls": outcome.tool_calls, "rejected": outcome.rejected,
            "steps_done": outcome.steps_done,
        })
        return outcome

    # ── 主体 ────────────────────────────────────

    def _loop(
        self,
        session: Any,
        *,
        run: KernelRun,
        store: KernelStore,
        mutable: _MutablePlan | None,
        spec: AgentSpec,
        mode: str,
        deterministic: bool,
        max_steps: int,
        strikes: dict[tuple[str, str], int],
        outcome: LoopOutcome,
    ) -> LoopOutcome:
        #: 本步内实际调用过的工具名。用来回答「计划指定了工具，它到底跑了没有」——
        #: 见 ``_close_step``：没跑就不能记 done。
        called: set[str] = set()

        while True:
            if outcome.rounds >= max_steps:
                raise LoopLimitError(
                    f"已达步数上限 {max_steps} 轮仍未收敛（mode={mode}；"
                    f"该模式的终止条件是"
                    f"{'计划跑完' if mode == PLAN_EXECUTE else '模型停止调用工具'}）。"
                    "继续跑只会重复消耗模型调用；请缩小任务范围，或把目标拆成多次会话。"
                )
            step = mutable.current_step() if mutable else None
            if mutable is not None and step is None:
                break  # 计划跑完
            outcome.rounds += 1

            response = self._decide(
                session, run=run, store=store, mutable=mutable, step=step,
                mode=mode, deterministic=deterministic, spec=spec,
            )
            calls = list(response.tool_calls or [])
            self._write_assistant(store, response)

            if not calls:
                if mutable is None:
                    if response.text.strip():
                        break
                    # 既没调工具也没说话，再问一次只会得到同样的空答复
                    raise KernelError(
                        "模型既未调用工具也未给出答复，循环无法推进", code="AGENT-LOOP-002",
                    )
                self._close_step(store, mutable, step, called, outcome)
                called = set()
                continue

            prepared = self._execute_batch(
                session, run=run, store=store, calls=calls, spec=spec,
                parallel=self.parallel and not deterministic,
            )
            for item in prepared:
                outcome.tool_calls += 1
                if item.status == "rejected":
                    outcome.rejected += 1
                called.add(item.call.name)
                # 回填与落库**按调用序**：乱序会让模型的因果推断错位 ——
                # 它看到结果 2 在结果 1 前面，只能假设调用顺序与自己发起的顺序不同，
                # 于是开始重排自己的推理步骤来「解释」这个顺序。
                self._write_tool_result(store, item)
                store.record_tool_call(item)

            exceeded = self._bump_strikes(prepared, strikes)
            if exceeded is not None:
                if mutable is not None:
                    mutable.fail_current()
                    store.save_plan(mutable.plan)
                raise StepFailureError(
                    f"{exceeded.call.name}(args={exceeded.key[1]}) 连续 {SELF_HEAL_LIMIT} 次"
                    f"失败，已终止该步转人工。最后一次错误：{exceeded.error}"
                )
        return outcome

    def _close_step(
        self,
        store: KernelStore,
        mutable: _MutablePlan,
        step: PlanStep | None,
        called: set[str],
        outcome: LoopOutcome,
    ) -> None:
        """收口一个计划步骤：**跑过才算 done**。

        计划步骤指定了工具、模型却直接给了文本时，这一步**没有执行任何动作**。
        把它记成 ``done`` 会让计划卡片显示「已完成」，而实际上什么都没有发生 ——
        这正是 D12「不把声称当执行」要挡的东西，只是发生在系统内部而不是界面上。
        记 ``skipped`` 并写下原因：用户看到的是「这一步跳过了」，而不是一个假的完成。
        """
        skipped = bool(step and step.tool and step.tool not in called)
        if skipped:
            outcome.skipped_steps += 1
            mutable.skip_current()
        else:
            outcome.steps_done += 1
            mutable.complete_current()
        store.save_plan(mutable.plan)
        store.emit(EVENT_PLAN_UPDATED, {
            "plan_id": outcome.plan_id, "mode": mutable.plan.mode, "status": "executing",
            "steps": mutable.plan.to_steps_payload(),
            **({"notes": [
                f"步骤 {step.id} 指定了工具 {step.tool}，但模型未调用它，已记为跳过"
            ]} if skipped else {}),
        })

    @staticmethod
    def _bump_strikes(
        outcomes: Sequence[CallOutcome],
        strikes: dict[tuple[str, str], int],
    ) -> CallOutcome | None:
        """累计失败次数。成功一次即把该键清零 —— D9 说的是**连续**失败。"""
        for item in outcomes:
            if item.status == "ok":
                strikes.pop(item.key, None)
                continue
            strikes[item.key] = strikes.get(item.key, 0) + 1
            if strikes[item.key] >= SELF_HEAL_LIMIT:
                return item
        return None

    # ── 决策 ────────────────────────────────────

    def _decide(
        self,
        session: Any,
        *,
        run: KernelRun,
        store: KernelStore,
        mutable: _MutablePlan | None,
        step: PlanStep | None,
        mode: str,
        deterministic: bool,
        spec: AgentSpec,
    ) -> ChatResponse:
        assembled = kernel_context.assemble(
            store.history(),
            system=_ROLE_PROMPT,
            plan=_driver(mutable.plan if mutable else None, step, mode, run.goal),
            budget_tokens=self.budget_tokens,
            recent_turns=self.recent_turns,
        )
        messages = [
            ChatMessage(
                role=m.role, content=m.content, tool_call_id=m.tool_call_id,
                tool_calls=list(m.tool_calls) if m.tool_calls else None,
            )
            for m in assembled.messages
        ]
        definitions = self.tools.tool_definitions(allowed=spec.tools)
        # 空清单传 None 而不是 []：``None`` 是「本次不带工具」，``[]`` 是「明确没有
        # 可用工具」，后者在部分端点会被判成参数非法。
        tools_arg = definitions or None

        def on_start(tier: str, candidates: list[dict]) -> None:
            store.emit(EVENT_LLM_START, {
                "tier": tier, "candidates": candidates,
                "budget_tokens": assembled.budget_tokens,
                "used_tokens": assembled.used_tokens,
                "kept_turns": assembled.kept_turns,
                "dropped_turns": assembled.dropped_turns,
                "collapsed_turns": assembled.collapsed_turns,
                "context_notes": list(assembled.notes),
                "tools": [d["function"]["name"] for d in (tools_arg or [])],
            })

        response = self.gateway.call(
            session,
            project_id=run.project_id, run_id=run.run_id,
            stage_id=run.stage_id, agent_id=run.agent_id, tier=run.tier,
            messages=messages, schema=None,
            max_tokens=self.max_tokens,
            temperature=0.0 if deterministic else DEFAULT_TEMPERATURE,
            tools=tools_arg,
            tool_choice=_tool_choice(step, tools_arg),
            seed=mutable.plan.seed if (mutable is not None and deterministic) else None,
            on_start=on_start,
        )
        if response.text.strip():
            # 非流式接入层下「一次调用 = 一次增量」：把整段文本作为一帧发出去。
            # ``streamed=false`` 明说了它不是逐 token 的增量 —— 不写这一位，
            # 前端会按「后面还有更多增量」去拼接，最终渲染出一段被截断的文本。
            store.emit(EVENT_ASSISTANT_DELTA, {
                "text": response.text, "final": True, "streamed": False,
                "provider": response.provider, "model": response.model,
            })
        return response

    # ── 工具执行 ────────────────────────────────

    def _execute_batch(
        self,
        session: Any,
        *,
        run: KernelRun,
        store: KernelStore,
        calls: Sequence[ToolCall],
        spec: AgentSpec,
        parallel: bool,
    ) -> list[CallOutcome]:
        """执行同一轮的多个调用，**结果按调用序返回**。

        ⚠️ **整轮要么都执行、要么都不执行**。闸门（``_gate``）跑在发出任何
        ``tool.call`` 之前：只要有一个调用需要人工批准，本轮就整体挂起，
        一个副作用都不产生。这不是洁癖 —— assistant 那条消息里的 ``tool_calls``
        是一个整体，只回填一半会让下一轮请求里出现「有调用没有结果」的配对，
        端点会直接拒收整条请求。恢复时整轮重放，因此也不会出现「同一次调用跑两遍」。
        """
        prepared = [self._prepare(call, spec=spec) for call in calls]
        self._gate(store, prepared)

        # 先把全部 ``tool.call`` 发出去：界面上「发起了 N 个调用」应当先于第一个结果
        # 出现，否则用户在慢工具上只会看到一片空白。
        for item in prepared:
            store.emit(EVENT_TOOL_CALL, {
                "call_id": item.call.id, "tool": item.call.name,
                "args": item.args, "permission": item.permission,
                "raw_arguments": item.call.arguments,
            })
        return self._run_batch(session, run=run, store=store, prepared=prepared,
                               spec=spec, parallel=parallel)

    def _gate(self, store: KernelStore, prepared: Sequence[CallOutcome]) -> None:
        """权限闸门（US-406 / §10.1）：判定要批就把整轮挂起。

        判据取**工具的静态** ``permission``（D5），不看模型说了什么 —— 让模型自称
        「这次是只读的」等于没有分级，而一个被注入污染的模型会立刻这样自称。

        ``granted`` 一次性从 store 取好再逐条判：每条各查一次库会在同一轮里问出
        同一份数据 N 遍，且两遍之间可能因为一次批准而不同 —— 那会让同一轮的
        N 个调用各自基于不同的世界状态被判定。
        """
        granted = store.approval_grants()
        round_calls: list[PendingCall] = []
        needs_approval = False
        for item in prepared:
            call_id = item.call.id or ""
            if item.status != "ok":
                # 已确定要被拒的（未注册 / 白名单外 / 参数坏）：它不会执行，也不需要批准。
                # 但它仍属于本轮 —— 一并带上，恢复时走同一条通道被拒并回填理由，
                # 否则那一轮的助手的 tool_calls 配对就永远差一条。
                round_calls.append(PendingCall(
                    call_id=call_id, tool_name=item.call.name, args=dict(item.args),
                    permission=item.permission, reason=item.error, needs_approval=False,
                ))
                continue
            spec = self.tools.get(item.call.name).spec
            decision = authorize(
                item.permission, tool_name=item.call.name, granted=granted,
                scope=scope_of(item.args, spec.scope_arg),
            )
            round_calls.append(PendingCall(
                call_id=call_id, tool_name=item.call.name, args=dict(item.args),
                permission=item.permission, reason=decision.reason,
                grant_key=decision.grant_key, needs_approval=decision.needs_approval,
            ))
            needs_approval = needs_approval or decision.needs_approval
        if needs_approval:
            raise ApprovalRequired(tuple(round_calls))

    def _run_batch(
        self,
        session: Any,
        *,
        run: KernelRun,
        store: KernelStore,
        prepared: list[CallOutcome],
        spec: AgentSpec,
        parallel: bool,
    ) -> list[CallOutcome]:
        """真正执行一批已通过闸门的调用，发 ``tool.result``，返回同一批对象。"""
        runnable = [item for item in prepared if item.status == "ok"]
        use_parallel = (
            parallel
            and len(runnable) > 1
            and self.session_factory is not None
            and all(item.permission == READ for item in runnable)
        )
        if use_parallel:
            results = self._run_parallel(session, run=run, items=runnable, spec=spec)
            for item, payload in zip(runnable, results, strict=True):
                _apply(item, payload)
        else:
            for item in runnable:
                ctx = self._context(session, run)
                _apply(item, self._invoke(item, ctx, spec))

        for item in prepared:
            store.emit(EVENT_TOOL_RESULT, {
                "call_id": item.call.id, "tool": item.call.name,
                "ok": item.status == "ok", "status": item.status,
                "duration_ms": item.duration_ms, "truncated": item.truncated,
                "error": item.error, "result_preview": _preview(item.output),
            })
        return prepared

    def execute_approved(
        self,
        session: Any,
        *,
        run: KernelRun,
        store: KernelStore,
        round_calls: Sequence[PendingCall],
        spec: AgentSpec,
        approval_id: int | None = None,
    ) -> list[CallOutcome]:
        """执行一轮**已获人工批准**的调用，并把结果写回对话（US-406）。

        恢复的语义是「重放这一轮」，不是「重跑这个作业」：计划与消息都还在库里，
        提示词不必重新拼，模型也不必重新问一次 —— 人批准的是**这一次调用**，
        重问一次模型很可能给出另一组调用，那样批准的对象就悄悄换了。

        ⚠️ 这里**不再过闸门**。已经批过了；再过一次只会因为「``dangerous`` 没有记忆」
        而立刻再挂起，形成「批准 → 又要求批准」的死循环。但**参数仍重新校验**：
        从挂起到批准之间可能过了几小时，工具的 schema 或白名单都可能已经变了。
        """
        prepared: list[CallOutcome] = []
        for pending in round_calls:
            call = ToolCall(
                id=pending.call_id, name=pending.tool_name,
                arguments=json.dumps(pending.args, ensure_ascii=False, sort_keys=True),
            )
            item = self._prepare(call, spec=spec)
            # 只有真的经过人工放行的那些才挂审批单号。同一轮里搭车的只读调用
            # 不需要批准，把它也记成「人批的」会让审批单看起来批了更多东西。
            if pending.needs_approval:
                item.approval_id = approval_id
            prepared.append(item)

        for item in prepared:
            store.emit(EVENT_TOOL_CALL, {
                "call_id": item.call.id, "tool": item.call.name,
                "args": item.args, "permission": item.permission,
                "raw_arguments": item.call.arguments,
                "approved": True, "approval_id": item.approval_id,
            })
        executed = self._run_batch(session, run=run, store=store, prepared=prepared,
                                   spec=spec, parallel=False)
        for item in executed:
            # 与 ``_loop`` 里的收尾逐字一致：回填顺序、落库时机都相同，
            # 让「正常执行」与「批准后执行」在审计表与消息流里长得一样。
            self._write_tool_result(store, item)
            store.record_tool_call(item)
        return executed

    def _prepare(self, call: ToolCall, *, spec: AgentSpec) -> CallOutcome:
        """解析参数、判白名单、取权限等级。**解析失败不静默当空参数**（US-409 的约定）。

        把「参数看不懂」当成「这次调用没有参数」，工具会带着默认行为跑起来 ——
        用户看到的是「工具执行成功了」，而它执行的是一件与模型意图不同的事。

        ⚠️ **白名单也在准备阶段判，而不是留到 ``_invoke``**。它与「未注册 / 参数坏」
        是同一类：**注定被拒的调用不该先惊动人**。留在执行期的话，闸门会为一次
        必然被拒的调用弹出一张批准卡，人批准了却什么也没发生 —— 而批准卡一旦开始
        出现「批了也没用」的项，人就会开始不看内容地点同意，审批疲劳就是这么来的。
        这与 ``registry.invoke`` 里的次序（先白名单、再 schema）也保持一致。
        """
        try:
            permission = self.tools.get(call.name).spec.permission
        except ToolError as exc:
            return CallOutcome(
                call=call, permission=UNKNOWN_PERMISSION, status="rejected", error=str(exc),
            )
        try:
            # 复用注册表自己的判定与措辞：两处各写一份白名单逻辑，迟早出现
            # 「闸门认为可以、执行时被拒」或反之。
            self.tools.ensure_allowed(call.name, spec.tools)
        except ToolError as exc:
            return CallOutcome(call=call, permission=permission, status="rejected", error=str(exc))
        try:
            args = call.parse_arguments()
        except ToolArgumentsError as exc:
            return CallOutcome(call=call, permission=permission, status="rejected", error=str(exc))
        return CallOutcome(call=call, args=args, permission=permission)

    @staticmethod
    def _context(session: Any, run: KernelRun) -> ToolContext:
        return ToolContext(
            session=session, project_id=run.project_id, job_id=run.job_id,
            agent_id=run.agent_id, conversation_id=run.conversation_id,
        )

    def _run_parallel(
        self, session: Any, *, run: KernelRun, items: Sequence[CallOutcome], spec: AgentSpec,
    ) -> list[Any]:
        """并发执行（仅只读工具），返回与 ``items`` **同序**的 ``_invoke`` 结果。

        ``anyio.run`` 需要一个没有事件循环在跑的线程。作业层已经保证执行段跑在
        ``anyio.to_thread`` 的工作线程里；万一有人从事件循环线程直接调进来，
        退回串行而不是抛错 —— 并行的收益只是省时间，不值得为它让整次对话失败。
        """
        def make(item: CallOutcome) -> Callable[[], Any]:
            def job() -> Any:
                borrowed = self.session_factory()
                try:
                    return self._invoke(item, self._context(borrowed, run), spec)
                finally:
                    with contextlib.suppress(Exception):
                        borrowed.rollback()
                    with contextlib.suppress(Exception):
                        borrowed.close()
            return job

        try:
            return _gather([make(item) for item in items])
        except RuntimeError as exc:  # 事件循环线程里没有并发可谈
            logger.warning("parallel_fallback_serial", error=str(exc))
            return [
                self._invoke(item, self._context(session, run), spec) for item in items
            ]

    def _invoke(
        self, item: CallOutcome, ctx: ToolContext, spec: AgentSpec,
    ) -> tuple[ToolResult | None, str | None, str]:
        """调注册表，并把两条失败通道折成同一种「模型可见的失败」。

        注册表把「工具失败」（``ok=False``）与「调用方违规」（抛 ``ToolError``）分开，
        面向的是**代码调用方**；而这里的调用方是模型，它能做的只有「换个工具」或
        「换组参数」—— 两类失败对它的处置完全一样，所以在这一层合流。
        审计表仍按 ``failed`` / ``rejected`` 分开记：对事后的人而言，
        「试过但没成」与「压根不该试」是完全不同的两件事。
        """
        try:
            result = self.tools.invoke(item.call.name, item.args, ctx, allowed=spec.tools)
        except ToolError as exc:
            return None, str(exc), "rejected"
        return result, result.error, "ok" if result.ok else "failed"

    # ── 落库 ────────────────────────────────────

    @staticmethod
    def _write_assistant(store: KernelStore, response: ChatResponse) -> None:
        """落 assistant 消息。**必须带 ``tool_calls``** —— 否则下一轮送回模型的
        ``tool`` 结果找不到它的调用，端点会直接拒收整条请求。"""
        calls = [c.to_dict() for c in (response.tool_calls or [])]
        if not calls and not response.text.strip():
            return  # 空答复不落库：它不携带信息，只会把上下文撑大并稀释有效内容
        store.append("assistant", response.text, tool_calls=calls or None)

    @staticmethod
    def _write_tool_result(store: KernelStore, item: CallOutcome) -> None:
        store.append("tool", item.content, tool_call_id=item.call.id or None)


# ── 计划游标 ────────────────────────────────────

class _MutablePlan:
    """计划的可变游标。

    计划本身是冻结的（``Plan`` 是 frozen dataclass），但执行过程中**各步状态**必须变 ——
    否则「从第几步继续」无从回答。这里只推进状态，不改步骤内容：改内容会让
    「批准即冻结」失效，检查点也就指不回当时那份计划了。
    """

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self._index = 0

    def current_step(self) -> PlanStep | None:
        """第一个未完成（且未失败）的步骤；顺带把它标成 ``running``。"""
        steps = list(self.plan.steps)
        while self._index < len(steps):
            step = steps[self._index]
            if step.status in ("done", "skipped"):
                self._index += 1
                continue
            if step.status != "running":
                steps[self._index] = dataclasses.replace(step, status="running")
                self.plan = dataclasses.replace(self.plan, steps=tuple(steps))
            return self.plan.steps[self._index]
        return None

    def complete_current(self) -> None:
        self._set("done")

    def fail_current(self) -> None:
        self._set("failed")

    def skip_current(self) -> None:
        """步骤没跑成但不该算「完成」时用它。

        与 ``fail_current`` 的区别：``failed`` 表示真的试过且失败了，``skipped`` 表示
        这一步压根没产生任何动作。两者都不能记 ``done``。
        """
        self._set("skipped")

    def _set(self, status: str) -> None:
        steps = list(self.plan.steps)
        if self._index < len(steps):
            steps[self._index] = dataclasses.replace(steps[self._index], status=status)
            self.plan = dataclasses.replace(self.plan, steps=tuple(steps))


# ── 纯函数辅助 ──────────────────────────────────

def _tool_choice(step: PlanStep | None, tools_arg: list[dict] | None) -> str | dict | None:
    """计划步骤指定了工具就**强制**到它身上（硬约束，不是建议）。

    只在该工具确实被下发时才强制：``tool_choice`` 指向一个不在 ``tools`` 里的名字，
    端点会直接 400 —— 而「计划要的工具不在白名单里」应当表现为这一轮没有可用工具，
    不该把整次对话打挂。
    """
    if step is None or not step.tool or not tools_arg:
        return "auto"
    available = {d["function"]["name"] for d in tools_arg}
    if step.tool not in available:
        logger.warning("plan_step_tool_not_available",
                       step=step.id, tool=step.tool, available=sorted(available))
        return "auto"
    return {"type": "function", "function": {"name": step.tool}}


def _driver(plan: Plan | None, step: PlanStep | None, mode: str, goal: str) -> str:
    """拼「当前该做什么」。它占据装配的**计划槽位**（裁剪梯队第二级，D8）。"""
    if plan is not None and step is not None:
        done = sum(1 for s in plan.steps if s.status in ("done", "skipped"))
        lines = [f"【当前计划】{plan.title or '（未命名）'}"]
        if goal.strip():
            lines.append(f"研究目标：{goal.strip()}")
        lines.append(f"进度：第 {done + 1}/{len(plan.steps)} 步")
        lines.append(f"当前步骤：{step.id} · {step.title}")
        if step.intent.strip():
            lines.append(f"该步意图：{step.intent.strip()}")
        if step.tool:
            lines.append(f"该步指定工具：{step.tool}")
        if step.params:
            lines.append("该步预设参数：" + json.dumps(
                step.params, ensure_ascii=False, sort_keys=True))
        lines.append("只推进**当前步骤**；该步做完时不再调用工具，直接说明这一步的结果。")
        return "\n".join(lines)
    if plan is not None:
        return f"【当前计划】{plan.title or '（未命名）'}\n计划已全部完成，直接汇报结果。"

    lines = ["【本会话没有生效中的计划】由你按研究目标自行决定下一步。"]
    if goal.strip():
        lines.append(f"研究目标：{goal.strip()}")
    lines.append("做完就停下来汇报；信息不足时先问，不要凭空假设。")
    return "\n".join(lines)


def _suggested_grant(gateway: Any, exc: BudgetExceeded) -> float:
    """审批单上展示的建议豁免额度。取不到就报 0，**绝不抛**。

    这里在异常处理路径上：为了一个展示字段再抛一次，会把「预算熔断 → 暂停 + 审批」
    变成一条看不出原因的 ``AttributeError``，而用户看到的是作业直接失败。
    兜底路径上的任何新异常，代价都远高于一个显示为 0 的建议值。
    """
    policy = getattr(getattr(gateway, "budget", None), "suggested_grant", None)
    if not callable(policy):
        return 0.0
    try:
        return float(policy(exc.kind))
    except Exception:  # noqa: BLE001 —— 见 docstring
        return 0.0


def _apply(item: CallOutcome, payload: tuple[ToolResult | None, str | None, str]) -> None:
    result, error, status = payload
    item.status = status
    item.error = error or ""
    if result is not None:
        item.output = result.output
        item.duration_ms = result.duration_ms
        item.truncated = result.truncated


def _preview(payload: Any, limit: int = 240) -> str:
    """给界面用的一小段结果预览。事件流里塞完整结果会让 ``job_events`` 迅速膨胀，
    而完整结果在 ``tool_calls`` 表里 —— 事件只需要够人认出「这是哪个结果」。"""
    if payload is None:
        return ""
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str
    )
    return text if len(text) <= limit else text[:limit] + "…"


def _gather(jobs: Sequence[Callable[[], Any]]) -> list[Any]:
    """并发跑一批同步任务，**按传入顺序返回结果**（``anyio`` 任务组本身不保序）。"""
    results: list[Any] = [None] * len(jobs)

    async def _main() -> None:
        async def _one(index: int, job: Callable[[], Any]) -> None:
            results[index] = await anyio.to_thread.run_sync(job)

        async with anyio.create_task_group() as group:
            for index, job in enumerate(jobs):
                group.start_soon(_one, index, job)

    anyio.run(_main)
    return results
