"""把内核循环接到真实的库上（US-405）。

``app/agent_kernel`` 的核心模块**不 import ``app.store``**（``Plan.from_row`` /
``ContextMessage.from_row`` 走鸭子类型，就是为了守住这条）。而循环多了一层需求：
读会话历史、写消息、写审计、发事件、推 run 状态 —— 全是写操作，且**必须能被逐条断言**。
这个文件做一件事：把「循环需要的世界」用窄接口供给它。

放在 ``app/orchestration/`` 而不是内核里的理由，与 ``run_pipeline`` 的注入一致
（第 4 步的契约选择）：方向是应用层 → 内核层，反过来 import 会成环。内核不认识这个文件，
它只认识 ``KernelStore`` 协议 —— 这也让循环的单测不需要数据库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.agent_kernel import checkpoints as kernel_checkpoints
from app.agent_kernel import context as kernel_context
from app.agent_kernel import permissions as kernel_permissions
from app.agent_kernel import planner
from app.agent_kernel import specs as kernel_specs
from app.agent_kernel.errors import ResumeError
from app.agent_kernel.loop import KernelRun
from app.agent_kernel.permissions import ApprovalRequired, PendingCall
from app.agent_kernel.tokens import estimate_message_tokens
from app.ai.base import ToolArgumentsError, ToolCall
from app.ai.budget import BudgetExceeded
from app.jobs.events import APPROVAL_REQUIRED, STAGE_PAUSED, emit
from app.store.dao import app_config as app_config_dao
from app.store.dao import approvals as approvals_dao
from app.store.dao import checkpoints as checkpoint_dao
from app.store.dao import conversations as conversations_dao
from app.store.dao import decisions as decisions_dao
from app.store.dao import jobs as jobs_dao
from app.store.dao import kernel_checkpoints as kernel_checkpoints_dao
from app.store.dao import messages as messages_dao
from app.store.dao import runs as runs_dao
from app.store.dao import task_plans as task_plans_dao
from app.store.dao import tool_calls as tool_calls_dao

#: 会话内核的运行可以执行哪些计划状态。**``draft`` 不在其中** ——
#: 批准即冻结（US-403 的契约）：没被批准的计划不该被执行，否则「计划」就只是一张
#: 随时会变的便签，而用户以为自己批过、或者以为自己还没批，都无从确认。
EXECUTABLE_PLAN_STATUSES = ("approved", "executing")


@dataclass
class ChatRun:
    """一次会话内核运行的装配结果：坐标 + 已经接好库的 store + 生效的 Agent 契约。

    ``agent_spec`` 与 ``spec``（``KernelRun``）是两回事，别混：前者是 **Agent 契约**
    （档位、白名单），后者是**这次运行的坐标**。批准后恢复要拿白名单重新校验参数，
    那时候装配点已经走远了 —— 少了这一份，恢复只能去猜该用谁的白名单。
    """

    spec: KernelRun
    store: SqlKernelStore
    agent_spec: kernel_specs.AgentSpec

    def __iter__(self):
        """让 ``run, store = chat_run(...)`` 这种写法也能用。"""
        yield self.spec
        yield self.store


class SqlKernelStore:
    """``KernelStore`` 协议的唯一实现。

    ``session`` 由作业层传进来：内核循环里所有写操作都走它，并在需要让 SSE 读端
    立刻看见的地方**自己 commit**（事件、run 行、暂停）—— 与 ``JobRunner`` 同一条
    事务约定。SQLite WAL 下未提交的数据在另一条连接上不可见。
    """

    def __init__(
        self,
        session: Session,
        *,
        project_id: int,
        conversation_id: int,
        run_id: int,
        agent_id: str,
        stage_id: str,
        job_id: int | None = None,
    ) -> None:
        self.session = session
        self.project_id = project_id
        self.conversation_id = conversation_id
        self.run_id = run_id
        self.agent_id = agent_id
        self.stage_id = stage_id
        self.job_id = job_id
        #: 本次运行取自哪一份计划（由 ``plan()`` 记下，供后续状态写回定位）
        self._plan_id: int | None = None

    # ── 读 ──────────────────────────────────────

    def history(self) -> list[kernel_context.ContextMessage]:
        rows = messages_dao.list_for_conversation(self.session, self.conversation_id)
        return [kernel_context.ContextMessage.from_row(row) for row in rows]

    def plan(self) -> planner.Plan | None:
        """生效中的计划：最近一份 ``approved`` / ``executing``。

        只看**最近一份**而不是「所有未完成的」：重规划会产生新行（``task_plans`` 的
        版本策略），而「当前计划」只能有一个答案 —— 有两个的话，界面显示的进度与实际
        执行的步骤会来自两份不同的计划，而且看起来都合理。
        """
        row = executable_plan_row(self.session, self.conversation_id)
        if row is None:
            return None
        self._plan_id = row.id
        return planner.Plan.from_row(row)

    # ── 写 ──────────────────────────────────────

    def append(
        self, role: str, content: str, *,
        tool_call_id: str | None = None,
        tool_calls: Any = None,
    ) -> None:
        payload = list(tool_calls) if tool_calls else None
        # token 估算走 ``ContextMessage`` 的同一条路径：它会连带把工具调用的参数算进去
        # （那些内容在回放时确实要占上游窗口）。自己再写一遍公式，迟早会算出两个数。
        calls: tuple[ToolCall, ...] | None = None
        if payload:
            parsed: list[ToolCall] = []
            for item in payload:
                try:
                    parsed.append(ToolCall.from_dict(item))
                except ToolArgumentsError:
                    continue
            calls = tuple(parsed) or None
        tokens = kernel_context.ContextMessage(
            role=role, content=content, tool_call_id=tool_call_id, tool_calls=calls,
        ).tokens
        messages_dao.create(
            self.session, conversation_id=self.conversation_id, role=role,
            content=content, tool_call_id=tool_call_id, tool_calls=payload, tokens=tokens,
        )

    def record_tool_call(self, outcome: Any) -> None:
        result = None
        if outcome.status == "ok":
            result = {"output": outcome.output} if not isinstance(
                outcome.output, dict
            ) else outcome.output
        tool_calls_dao.create(
            self.session,
            conversation_id=self.conversation_id,
            run_id=self.run_id,
            tool_name=outcome.call.name,
            args=outcome.args,
            permission=outcome.permission,
            status=outcome.status,
            result=result,
            error=outcome.error,
            duration_ms=outcome.duration_ms,
            approval_id=getattr(outcome, "approval_id", None),
        )

    def save_plan(self, plan: planner.Plan) -> None:
        plan_id = plan.plan_id or self._plan_id
        if plan_id is None:
            return
        self._plan_id = plan_id
        task_plans_dao.save_progress(
            self.session, plan_id, steps=plan.to_steps_payload(),
        )

    def set_plan_status(self, status: str) -> None:
        plan_id = self._plan_id
        if plan_id is None:
            row = task_plans_dao.current_for_conversation(self.session, self.conversation_id)
            plan_id = row.id if row is not None else None
        if plan_id is None:
            return
        self._plan_id = plan_id
        task_plans_dao.set_status(self.session, plan_id, status)

    def set_run_status(self, status: str, error: str | None = None) -> None:
        runs_dao.finish_run(self.session, run_id=self.run_id, status=status, error=error)
        if status == "failed" and error:
            # 失败尝试要留一条决策日志（与 Orchestrator 一致）：回放时能答出
            # 「这次是为什么没成」，而不是只看到一个 agent_runs.status=failed。
            decisions_dao.add(
                self.session, project_id=self.project_id, run_id=self.run_id,
                stage_id=self.stage_id, agent_id=self.agent_id,
                decision="会话内核运行失败", reason=error, kind="failed_attempt",
            )
        self.session.commit()

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.job_id is None:
            return
        try:
            emit(self.session, self.job_id, event_type, payload)
        except Exception:  # noqa: BLE001 —— 发事件失败不该盖掉真正的失败原因
            self.session.rollback()

    def mark_step(self, kind: str, content: dict[str, Any]) -> None:
        """记一步（``agent_steps``，US-407）。

        在这之前**会话路径一步都不记**，于是 ``BudgetManager`` 里那道
        ``agent_steps`` 闸门在会话里形同虚设：同一个 Agent 走阶段作业会被限步，
        走聊天不受限，而且没有任何地方能看出来。``runs_dao.add_step`` 顺带把
        ``agent_runs.steps`` 推到最新序号 —— 闸门读的正是它。
        """
        runs_dao.add_step(self.session, run_id=self.run_id, kind=kind, content=content)

    def save_checkpoint(self, checkpoint: kernel_checkpoints.Checkpoint) -> None:
        """落一条内核检查点（US-407）。

        **必须提交**：检查点的全部用途都发生在「进程已经没了」之后（崩溃恢复、
        中断续跑），留在未提交的事务里等于没写 —— 而「没写」的表现恰好是
        「用户以为可以续跑，点下去却被告知没有任何检查点」。
        """
        kernel_checkpoints_dao.create(
            self.session,
            conversation_id=self.conversation_id,
            # 计划行可能已被删（会话被删时 ``plan_id`` 置 NULL），而循环手上那份
            # ``outcome.plan_id`` 还是旧的；以能查到的那份为准。
            plan_id=checkpoint.plan_id if checkpoint.plan_id is not None else self._plan_id,
            step_index=checkpoint.step_index,
            status=checkpoint.status,
            snapshot=dict(checkpoint.snapshot),
        )
        self.session.commit()

    def cancel_requested(self) -> bool:
        """有人请求中止这次运行吗（US-407 协作式取消）。

        用**列查询**而不是 ``session.get(Job, …)``：后者会命中身份映射里的旧对象，
        而写这个标记的是另一个线程的另一个 session —— 读到缓存的话，用户按了停止
        而循环永远看不见，表现为「点了没反应」。
        """
        if self.job_id is None:
            return False
        return jobs_dao.cancel_requested(self.session, self.job_id)

    def approval_grants(self) -> set[str]:
        """已生效的审批记忆键（US-406 / D5）。

        存在 ``app_config`` 而不是进程内存里，是为了跨重启有效：只记内存的话，
        用户昨天批准过的动作今天会再要一遍批准，而「刚批过又要批」正是审批疲劳的
        来源 —— 疲劳之后，人会在不看内容的情况下点同意。
        """
        stored = app_config_dao.get_prefix(self.session, kernel_permissions.GRANT_PREFIX)
        # ``get_prefix`` 返回的是**去掉前缀**的键，这里补回去：闸门比较的是完整键，
        # 少补这一步，集合里每一个都不命中，表现为「批准了但下次还要批」。
        return {f"{kernel_permissions.GRANT_PREFIX}{name}" for name in stored}

    def pause_for_approval(
        self, exc: ApprovalRequired, *, checkpoint: kernel_checkpoints.Checkpoint,
    ) -> None:
        """需要人工批准 → 暂停 + 审批单（US-406 / D4：复用 approvals 表，kind=dangerous）。

        与 ``pause_for_budget`` 逐项对齐：run 置 paused、开一张审批单、存现场、发事件。
        内核另做一套的话，用户会在同一个界面上看到两种形状的审批卡。

        ``detail`` 里同时带 ``pending``（要批的）与 ``round``（整轮全部）：
        后者是**恢复时的执行清单** —— 挂起的是一整轮，只重放需要批准的那几条，
        assistant 那条消息里的 ``tool_calls`` 就永远配不齐结果。
        """
        runs_dao.finish_run(self.session, run_id=self.run_id, status="paused")
        row = approvals_dao.create(
            self.session, project_id=self.project_id,
            kind=kernel_permissions.APPROVAL_KIND_DANGEROUS, run_id=self.run_id,
            detail={
                "source": "tool_permission",
                "stage_id": self.stage_id,
                "agent_id": self.agent_id,
                "conversation_id": self.conversation_id,
                **exc.detail(),
            },
        )
        # 先 flush 拿到 ``id``：approval.required 事件要带上它，界面才能把
        # 「这条待批准」直接指到审批单上；不带的话前端只能按 (run_id, kind) 反查，
        # 而同一个 run 前后可能暂停过不止一次。
        self.session.flush()
        approval_id = row.id
        checkpoint_dao.save_checkpoint(
            self.session, project_id=self.project_id, stage_id=self.stage_id,
            status="paused",
            snapshot={
                "stage_id": self.stage_id, "run_id": self.run_id,
                "conversation_id": self.conversation_id,
                "reason": "tool_permission", "approval_id": approval_id,
            },
        )
        # 内核检查点补上「为什么停」与审批单号：续跑时它要能自己回答
        # 「这次暂停是在等哪一张单子」，否则用户只能去翻审批列表。
        self._kernel_checkpoint(
            checkpoint, status=kernel_checkpoints.PAUSED,
            extra={"reason": "tool_permission", "approval_id": approval_id},
        )
        self.session.commit()
        self.emit(STAGE_PAUSED, {
            "run_id": self.run_id, "reason": "tool_permission",
            "conversation_id": self.conversation_id, "approval_id": approval_id,
        })
        self.emit(APPROVAL_REQUIRED, {
            "run_id": self.run_id, "approval_id": approval_id,
            "conversation_id": self.conversation_id,
            "reason": exc.reason,
            "pending": [call.to_dict() for call in exc.pending],
        })

    def pause_for_budget(
        self, exc: BudgetExceeded, *, suggested_grant: float,
        checkpoint: kernel_checkpoints.Checkpoint,
    ) -> None:
        """预算熔断 → 暂停 + 审批单（§8.5 的既有语义，内核复用而不是另起一套，D4）。

        与 ``Orchestrator.run_stage`` 的处置逐项对齐：run 置 paused、开一张
        ``kind="budget"`` 的审批单（带建议豁免额度）、存检查点、发暂停事件。
        内核另做一套的话，用户会在同一个界面上看到两种形状的审批卡。
        """
        runs_dao.finish_run(self.session, run_id=self.run_id, status="paused")
        approvals_dao.create(
            self.session, project_id=self.project_id, kind="budget", run_id=self.run_id,
            detail={
                "source": exc.kind,
                "stage_id": self.stage_id,
                "agent_id": self.agent_id,
                "conversation_id": self.conversation_id,
                "suggested_grant": suggested_grant,
                **exc.detail,
            },
        )
        checkpoint_dao.save_checkpoint(
            self.session, project_id=self.project_id, stage_id=self.stage_id,
            status="paused",
            snapshot={
                "stage_id": self.stage_id, "run_id": self.run_id,
                "conversation_id": self.conversation_id, "reason": exc.kind,
            },
        )
        self._kernel_checkpoint(
            checkpoint, status=kernel_checkpoints.PAUSED,
            extra={"reason": exc.kind},
        )
        self.session.commit()
        self.emit(STAGE_PAUSED, {
            "run_id": self.run_id, "reason": exc.kind,
            "conversation_id": self.conversation_id,
        })

    def pause_for_cancel(
        self, *, checkpoint: kernel_checkpoints.Checkpoint, reason: str = "cancelled",
    ) -> None:
        """应人请求中止 → 暂停 + 检查点（US-407）。

        出路与另外两个暂停**完全相同**（``agent_runs.status = paused``），只是
        **不开审批单**：中止是人的指令，没有什么需要再批一遍的；开一张空单子只会让
        审批列表里多出一条永远不该被处理的记录。

        落 ``paused`` 而不是 ``failed`` 是这段代码的全部要点：落 failed 的话
        ``JobRunner._terminal_status`` 会把它带成 ``job.failed``，而用户唯一的出路
        变成重来 —— 已经做完的那几步就白做了，而这恰恰是「检查点续跑」要解决的问题。

        ⚠️ **``jobs.cancel_requested`` 刻意不清除**。它身兼两职：既是循环要读的信箱，
        也是事后判「这次暂停是不是因为中止」的凭据 —— ``JobRunner._pause_reason``
        在循环返回**之后**才跑，那时已经没有别的线索能区分「中止」与「不知为何暂停」。
        清掉它，「已中止」在界面上会退化成 ``unknown``。
        留着也不会有副作用：暂停后的作业已是终态，``run_job`` 不会再跑它第二遍；
        而**续跑走的是新的一行作业**，标记天然是干净的。
        """
        runs_dao.finish_run(self.session, run_id=self.run_id, status="paused")
        self._kernel_checkpoint(
            checkpoint, status=kernel_checkpoints.CANCELLED, extra={"reason": reason},
        )
        self.session.commit()
        self.emit(STAGE_PAUSED, {
            "run_id": self.run_id, "reason": reason,
            "conversation_id": self.conversation_id,
        })

    def _kernel_checkpoint(
        self,
        checkpoint: kernel_checkpoints.Checkpoint,
        *,
        status: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """落一条**内核**检查点（US-407）。与上面的 ``checkpoint_dao``（阶段检查点）
        是两张表：那张按 ``(project_id, stage_id)`` 定位、服务阶段级重跑，这张按
        ``conversation_id``、服务「一次会话跑到一半停下」。

        暂停路径用它把「为什么停」补进快照 —— 内核那边只知道「我要暂停了」，
        审批单号与熔断种类只有这一层才知道。
        """
        snapshot = dict(checkpoint.snapshot)
        if extra:
            snapshot.update(extra)
        kernel_checkpoints_dao.create(
            self.session,
            conversation_id=self.conversation_id,
            plan_id=checkpoint.plan_id if checkpoint.plan_id is not None else self._plan_id,
            step_index=checkpoint.step_index,
            status=status or checkpoint.status,
            snapshot=snapshot,
        )


# ── 装配 ────────────────────────────────────────

def executable_plan_row(session: Session, conversation_id: int):
    """本会话当前**可执行**的那份计划行（``approved`` / ``executing``），没有则 ``None``。

    ``list_for_conversation`` 已是新在前，取第一条命中的即「最近的一份」。
    """
    for row in task_plans_dao.list_for_conversation(session, conversation_id):
        if row.status in EXECUTABLE_PLAN_STATUSES:
            return row
    return None


def resume_decision(
    session: Session, conversation_id: int,
) -> kernel_checkpoints.ResumeDecision:
    """「这个会话能不能续跑、从哪续」（US-407）：查库 + 判定。

    **查库在应用层、判定在内核**（``checkpoints.decide_resume`` 是纯函数）：
    内核不认识库，而这道判定恰恰最需要逐种状态断言。

    只看**最近的**一条检查点：更早的那些记录的是上一轮之前的状态，
    拿它们判断会答出「上上次停在哪」。
    """
    latest = kernel_checkpoints_dao.latest_for_conversation(session, conversation_id)
    checkpoint = (
        kernel_checkpoints.Checkpoint.from_row(latest) if latest is not None else None
    )
    row = executable_plan_row(session, conversation_id)
    plan = planner.Plan.from_row(row) if row is not None else None
    return kernel_checkpoints.decide_resume(checkpoint, plan)


def open_chat_run(
    session: Session,
    *,
    project_id: int,
    conversation_id: int,
    job_id: int | None = None,
    agent_id: str | None = None,
    goal: str = "",
) -> ChatRun:
    """开一次会话内核运行：解析契约、建 run 行、装好 store。

    ``agent_id`` 决定工具白名单（§5.3 最小权限）。缺省用会话内核自己的契约
    （``CONVERSATION_SPEC``，工具面最小）；要跑真实实验必须显式声明 ``executor`` ——
    「默认最严、提权要写明」正是权限最小化的落地方式。

    未知 ``agent_id`` **抛错而不是回退缺省**：默默换成缺省契约会让「我指定了 executor
    却拿到的是一组更少的工具」表现为「工具不见了」，而不是「这个名字不存在」。
    """
    if agent_id:
        spec = kernel_specs.by_agent_id(agent_id)
        if spec is None:
            known = "、".join(sorted(kernel_specs.BY_AGENT_ID))
            raise ValueError(f"未知 agent_id：{agent_id}（可选 {known}）")
    else:
        spec = kernel_specs.CONVERSATION_SPEC

    if conversations_dao.get(session, conversation_id) is None:
        raise ValueError(f"会话不存在：{conversation_id}")

    run = runs_dao.create_run(
        session, project_id=project_id, stage_id=spec.stage, agent_id=spec.id,
    )
    session.commit()  # run 行先落地：SSE 读端是独立连接，未提交的它看不见
    store = SqlKernelStore(
        session, project_id=project_id, conversation_id=conversation_id,
        run_id=run.id, agent_id=spec.id, stage_id=spec.stage, job_id=job_id,
    )
    return ChatRun(
        spec=KernelRun(
            project_id=project_id, conversation_id=conversation_id, run_id=run.id,
            agent_id=spec.id, stage_id=spec.stage, tier=spec.tier,
            job_id=job_id, goal=goal,
        ),
        store=store,
        agent_spec=spec,
    )


def estimate_tokens(role: str, content: str) -> int:
    """给 API 层用的一行转调，避免它去 import 内核的 tokens 模块。"""
    return estimate_message_tokens(role, content)


def run_chat_job(
    loop: Any,
    session: Session,
    *,
    project_id: int,
    params: dict[str, Any],
    job_id: int,
) -> int:
    """``kind=chat`` 作业的处理体（注入给 ``JobRunner``）。返回 run_id。

    ``params`` 里带 ``conversation_id``（必填）与可选的 ``agent_id`` / ``goal``。
    缺 ``conversation_id`` **直接抛错**：猜一个会话比报错危险得多 ——
    用户会看到另一个会话里凭空多出一轮对话。

    **``approve_calls`` 是「批准后恢复」的入口（US-406）**：审批 + 执行
    不在 HTTP 请求里同步做完，而是回到这条已验证过的作业通道上 ——
    SSE 断线续传、取消、僵尸作业自愈都已经在这里跑过一遍，另开一条会分裂语义。

    **``resume=True`` 是「从检查点续跑」的入口（US-407）**，也走同一条通道：
    续跑无非是「再开一次运行，而这次的起点不是第一轮」。在这里重判一次而不是相信
    API 层的结论 —— 从受理到真正开跑之间隔着一条队列，这期间计划可能又被推进过。

    这里不吞任何异常：作业层的兜底负责把失败落成 ``job.failed``，
    而内核错误的码写在 ``str(exc)`` 里，一路带到事件与 ``agent_runs.error``。
    """
    conversation_id = params.get("conversation_id")
    if not conversation_id:
        raise ValueError("chat 作业缺少 params.conversation_id")
    chat = open_chat_run(
        session,
        project_id=project_id,
        conversation_id=int(conversation_id),
        job_id=job_id,
        agent_id=params.get("agent_id"),
        goal=str(params.get("goal") or ""),
    )

    decision: kernel_checkpoints.ResumeDecision | None = None
    if params.get("resume"):
        decision = resume_decision(session, int(conversation_id))
        if not decision.resumable:
            # **不降级成「从头再跑一遍」**：用户点的是「继续」，从头跑会让他看到
            # 重复的动作，而且已经产生过的副作用（写过文件、发过请求）会再来一次。
            raise ResumeError(f"无法续跑：{decision.reason}")

    approved = params.get("approve_calls") or []
    if approved:
        # 只有**拿到内容**才恢复。空列表是「这次没有要重放的调用」，与
        # 「恢复了但一条都没执行」在审计里必须长得不一样。
        _replay_approved_calls(
            loop, session, chat, approved,
            approval_id=params.get("approval_id"),
        )

    # ⚠️ ``spec=chat.agent_spec`` 必须显式传。不传的话 ``run`` 回落到 ``loop.spec``
    # （装配时绑的 ``CONVERSATION_SPEC``），于是**同一个 run 里出现两份契约**：
    # 重放那一轮按 ``agent_id`` 解析出的白名单执行，续跑那一轮按会话内核的白名单判定。
    # 表现为「明明提权到 executor 了，下一步却说自己不允许读文件」——
    # 而且 ``agent_runs.agent_id`` 写的是 executor，审计上看起来权限一直都在。
    loop.run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
        resume=decision,
    )
    return chat.spec.run_id


def _replay_approved_calls(
    loop: Any,
    session: Session,
    chat: ChatRun,
    approved: list[dict[str, Any]],
    *,
    approval_id: int | None,
) -> None:
    """重放人工批准的那一轮调用，并把结果写回对话（US-406）。

    先把它 `commit` 再让循环接管：``loop.run`` 会另起一轮去模型那边，
    而它读的是**数据库里**的历史 —— 没提交的话，模型这一轮看到的上下文里
    没有刚执行出来的工具结果，于是会把同一件事再做一遍。
    """
    round_calls = tuple(PendingCall.from_dict(item) for item in approved)
    loop.execute_approved(
        session, run=chat.spec, store=chat.store, round_calls=round_calls,
        spec=chat.agent_spec, approval_id=approval_id,
    )
    session.commit()
