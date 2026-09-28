"""执行控制：检查点、协作式中止与续跑（US-407）。

这一份测试要钉住的是 G2 的第 4、5 两条：

- **第 4 条**：预算超限能暂停并恢复
- **第 5 条**：中断后能从检查点继续、**步骤序号连续**

「步骤序号连续」听起来像句废话，其实是最容易悄悄坏掉的一条：续跑一旦把序号从 1 重新数
起，或者把已经做完的第 2 步又做一遍，**界面上不会有任何报错** —— 用户看到的是一次
「跑得有点久」的正常执行。所以下面既断言序号本身（单调不减），也断言「做过的事没有
被再做一次」（第二次运行只记了一步）。

分三层，与实现的分层一一对应：

1. **纯函数**（``checkpoints.decide_resume``）——「能不能续、从哪续」的全部判断。
   不碰数据库：每种状态各断一次，这是唯一能覆盖到「检查点坏了」这类输入的办法。
2. **循环 + 真库**（``KernelLoop`` + ``SqlKernelStore``）—— 检查点真的落了库、
   步数真的推了 ``agent_runs.steps``、中止真的在安全点生效。
3. **HTTP 端到端** —— ``POST /api/conversations/{id}/resume`` 一路走到计划收口。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.agent_kernel import checkpoints as kernel_checkpoints
from app.agent_kernel import planner
from app.agent_kernel.loop import KernelLoop
from app.agent_kernel.specs import AgentSpec
from app.agent_kernel.tools.registry import ToolRegistry
from app.ai.base import ChatResponse
from app.ai.budget import BudgetExceeded, BudgetManager
from app.main import create_app
from app.orchestration import kernel_store
from app.store.dao import agents as agents_dao
from app.store.dao import approvals as approvals_dao
from app.store.dao import conversations as conversations_dao
from app.store.dao import grants as grants_dao
from app.store.dao import jobs as jobs_dao
from app.store.dao import kernel_checkpoints as checkpoints_dao
from app.store.dao import projects as projects_dao
from app.store.dao import runs as runs_dao
from app.store.dao import task_plans as task_plans_dao

S1, S2 = "s1", "s2"


# ── 一、纯函数：能不能续、从哪续 ────────────────

def _plan(*statuses: str, plan_id: int | None = 7) -> planner.Plan:
    """按状态造一份计划；步骤 id 固定是 s1/s2/…，与断言里的字面量对得上。"""
    steps = tuple(
        planner.PlanStep(id=f"s{i}", title=f"第 {i} 步", status=status)
        for i, status in enumerate(statuses, start=1)
    )
    return planner.Plan(steps=steps, mode=planner.PLAN_EXECUTE, plan_id=plan_id)


def _checkpoint(status: str, *, plan: planner.Plan | None, **snapshot) -> kernel_checkpoints.Checkpoint:
    base = {"mode": planner.PLAN_EXECUTE, "rounds": 0, "next_step_id": None, "reason": ""}
    base.update(snapshot)
    if plan is not None and "next_step_id" not in snapshot:
        base["next_step_id"] = kernel_checkpoints.next_step_id(plan)
    return kernel_checkpoints.Checkpoint(
        conversation_id=1, plan_id=plan.plan_id if plan else None, step_index=0,
        status=status, snapshot=base,
    )


def test_without_any_checkpoint_there_is_nothing_to_resume():
    decision = kernel_checkpoints.decide_resume(None, _plan("done", "done"))
    assert decision.resumable is False
    assert "没有任何检查点" in decision.reason


def test_a_finished_run_is_not_resumable_only_rerunnable():
    """``done`` 之后再点「继续」是**重跑**，不是续跑。

    这一条是负向的，但它防的是真实的破坏：把一份已经跑完的计划从头再跑一遍，
    已经做过的副作用（写过文件、发过请求）会全部再来一次。
    """
    decision = kernel_checkpoints.decide_resume(
        _checkpoint(kernel_checkpoints.DONE, plan=_plan("done", "done")), _plan("done", "done"),
    )
    assert decision.resumable is False
    assert "已经跑完" in decision.reason


def test_an_unrecognisable_status_is_refused_instead_of_guessed():
    decision = kernel_checkpoints.decide_resume(
        _checkpoint("halfway", plan=_plan("done")), _plan("done"),
    )
    assert decision.resumable is False
    assert "halfway" in decision.reason


@pytest.mark.parametrize(
    "status", [kernel_checkpoints.RUNNING, kernel_checkpoints.PAUSED, kernel_checkpoints.CANCELLED],
)
def test_every_unfinished_status_is_resumable(status):
    """``running`` 也必须在列：上一进程死在半路时留下的就是它（来不及写终态）。

    少了它，「服务崩过一次」= 这个会话永久报废。
    """
    plan = _plan("done", "running", "pending")
    decision = kernel_checkpoints.decide_resume(_checkpoint(status, plan=plan), plan)
    assert decision.resumable is True
    assert decision.start_step_id == S2, decision
    assert decision.step_index == 2, "序号是给人读的，它该是「第 2 步」"


def test_a_failed_step_is_still_the_resume_point():
    """``failed`` 不是「已经过去」：那一步没做成，续跑要重新面对它。

    它与 ``skipped`` 的区别正在这里 —— 跳过是「这一步压根没产生动作」，
    而失败是「试过、没成」，两者对后续推理的意义完全不同。
    """
    plan = _plan("done", "failed", "pending")
    assert kernel_checkpoints.next_step_id(plan) == S2
    assert kernel_checkpoints.decide_resume(_checkpoint("paused", plan=plan), plan).start_step_id == S2


def test_resume_looks_up_the_step_by_id_not_by_position():
    """中间插一步，续跑点不该跟着挪位 —— US-403 那条铁律的直接检验。

    用下标定位的话，这里会得到「第 2 步」，而计划里第 2 步已经换成另一件事了。
    错位之后循环**不会报错**，它会一本正经地把错的那一步跑完。
    """
    plan = planner.Plan(
        steps=(
            planner.PlanStep(id="intro", title="铺垫", status="done"),
            planner.PlanStep(id="inserted", title="后插进来的一步", status="pending"),
            planner.PlanStep(id="body", title="正文", status="pending"),
        ),
        plan_id=7,
    )
    checkpoint = kernel_checkpoints.Checkpoint(
        conversation_id=1, plan_id=7, step_index=2, status=kernel_checkpoints.CANCELLED,
        snapshot={"next_step_id": "body"},
    )
    decision = kernel_checkpoints.decide_resume(checkpoint, plan)
    # 计划说该跑 inserted（它排在最前），检查点说 body —— 分歧记下来，但**以计划为准**：
    # 计划是活的事实，检查点只是上一次运行末尾的一张照片。
    assert decision.resumable is True
    assert decision.start_step_id == "inserted"
    assert decision.stale is True
    assert "以计划为准" in decision.reason


def test_react_mode_resumes_on_the_counter_alone():
    """``react`` 没有计划可定位，续跑的全部含义就是「带着已跑的轮数继续问模型」。"""
    checkpoint = kernel_checkpoints.Checkpoint(
        conversation_id=1, plan_id=None, step_index=0,
        status=kernel_checkpoints.CANCELLED,
        snapshot={"mode": planner.REACT, "rounds": 4, "steps_done": 0, "next_step_id": None},
    )
    decision = kernel_checkpoints.decide_resume(checkpoint, None)
    assert decision.resumable is True
    assert decision.start_step_id is None and decision.step_index == 0
    assert decision.rounds == 4, "上次跑了几轮要带出来，否则「跑了多少轮」会凭空缩水"


def test_a_finished_plan_resumes_straight_into_wrapping_up():
    plan = _plan("done", "done")
    decision = kernel_checkpoints.decide_resume(_checkpoint("cancelled", plan=plan), plan)
    assert decision.resumable is True
    assert decision.start_step_id is None
    assert decision.step_index == 2


def test_step_index_is_zero_when_it_cannot_be_located():
    """找不到就报 0，不瞎猜一个位置出来 —— 猜出来的序号会让人以为续跑点是对的。"""
    plan = _plan("pending")
    assert kernel_checkpoints.step_index_of(plan, "不存在") == 0
    assert kernel_checkpoints.step_index_of(None, S1) == 0
    assert kernel_checkpoints.step_index_of(plan, None) == 1


def test_the_snapshot_always_carries_every_agreed_key():
    """快照的键由 ``SNAPSHOT_KEYS`` 定，写入方只准用 ``make_snapshot``。

    少一个键的后果不是报错，而是**某条恢复路径只能靠猜**：少了 ``rounds``，
    「这个会话一共花了多少轮」在恢复之后就再也答不上来。
    """
    snapshot = kernel_checkpoints.make_snapshot(
        _plan("done", "pending"), mode=planner.PLAN_EXECUTE, rounds=3,
    )
    assert set(snapshot) == set(kernel_checkpoints.SNAPSHOT_KEYS)
    assert snapshot["next_step_id"] == S2


def test_strikes_survive_a_json_round_trip_and_junk_is_dropped():
    """自愈计数的键是元组，JSON 存不下，得自己序列化。

    读回来时**坏数据一律丢弃**：这条路径在恢复的关键路径上，为几个字节崩掉，
    代价是整个会话续不了；而丢计数最坏只是少一层保护（还有 ``max_steps`` 兜着）。
    """
    strikes = {("read_file", "ab12"): 2, ("run_command", "cd34"): 1}
    dumped = kernel_checkpoints.dump_strikes(strikes)
    assert json.loads(json.dumps(dumped)) == dumped, "必须能原样进 JSON 列"
    assert kernel_checkpoints.parse_strikes(dumped) == strikes

    assert kernel_checkpoints.parse_strikes("不是列表") == {}
    assert kernel_checkpoints.parse_strikes([["只有两项", "x"], ["tool", "hash", "不是数字"]]) == {}
    assert kernel_checkpoints.parse_strikes([["tool", "hash", 3]]) == {("tool", "hash"): 3}


# ── 二、循环 + 真库 ─────────────────────────────

class ScriptedGateway:
    """按脚本返回响应；脚本用完还问 = 循环没收敛，直接失败。

    ``after_first`` 在**第一次**回答被取走之后调用 —— 需要在中途改变世界的测试
    （「刚跑完第一步，有人按了停止」）靠它拿到那个时机。用真实线程去卡这个点
    会得到一条偶尔失败的测试。
    """

    def __init__(self, responses, *, after_first=None, budget=None, run_coords=None) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.after_first = after_first
        #: 传 ``BudgetManager`` 时每次都真的过一遍闸门（见 ``call``）；不传就用个空壳
        self.budget = _Budget() if budget is None else budget
        #: 过闸门要的坐标 ``(project_id, agent_id, run_id)``
        self.run_coords = run_coords

    def call(self, session, **kwargs):
        if isinstance(self.budget, BudgetManager):
            project_id, agent_id, run_id = self.run_coords
            self.budget.check(session, project_id, agent_id, run_id)
        self.calls.append(kwargs)
        if kwargs.get("on_start") is not None:
            kwargs["on_start"](kwargs["tier"], [{"provider": "fake", "model": "m"}])
        assert self.responses, "模型被多问了一轮（循环没有收敛）"
        response = self.responses.pop(0)
        if self.after_first is not None and len(self.calls) == 1:
            self.after_first(session)
        return response


class _Budget:
    def suggested_grant(self, kind: str) -> float:
        return 5.0


def reply(text: str = "") -> ChatResponse:
    return ChatResponse(text=text, provider="fake", model="m", tool_calls=None)


def _loop(gateway, *, max_steps: int = 8, session_factory=None) -> KernelLoop:
    return KernelLoop(
        gateway=gateway,
        tools=ToolRegistry(),
        spec=AgentSpec(id="kernel", stage="chat", tier="plan", tools=(), max_steps=max_steps),
        session_factory=session_factory,
    )


def _setup(session, *step_statuses: str):
    """造一个项目 + 会话 + 一份**已批准**的计划 + 一个作业行。"""
    project = projects_dao.create(session, title="执行控制", goal="把三步做完")
    conversation = conversations_dao.create(session, project_id=project.id, title="跑一遍")
    session.flush()
    payload = [
        planner.PlanStep(id=f"s{i}", title=f"第 {i} 步", status=status).to_dict()
        for i, status in enumerate(step_statuses, start=1)
    ]
    plan = task_plans_dao.create(
        session, conversation_id=conversation.id, steps=payload,
        mode=planner.PLAN_EXECUTE, deterministic=True, seed=0,
        title="测试计划", status="approved",
    )
    job = jobs_dao.create(
        session, project_id=project.id, kind="chat",
        params={"conversation_id": conversation.id},
    )
    session.commit()
    chat = kernel_store.open_chat_run(
        session, project_id=project.id, conversation_id=conversation.id, job_id=job.id,
    )
    return project, conversation, plan, job, chat


def _rows(session, conversation_id: int):
    """按**写入顺序**（旧 → 新）取检查点。DAO 给的是新在前，这里翻过来读起来顺。"""
    return list(reversed(checkpoints_dao.list_for_conversation(session, conversation_id)))


def _new_job(session, project_id: int, conversation_id: int):
    """开一行新的会话作业。

    续跑**必须换一行作业**，这不是测试的方便法门而是生产的真实形状：会话作业跑完/暂停后
    就是终态，``JobRunner.run_job`` 不会再跑它第二遍（``is_terminal`` 直接返回）。
    用同一个 job 去续跑，等于让它带着「上次那声停止」的标记重新开跑 —— 而标记是
    **不被清除的**（见 ``SqlKernelStore.pause_for_cancel``）。
    """
    job = jobs_dao.create(
        session, project_id=project_id, kind="chat",
        params={"conversation_id": conversation_id},
    )
    session.commit()
    return job


def test_every_round_records_a_step_and_a_checkpoint(session):
    """每轮都要留下两样东西：一步（``agent_steps``）与一条检查点。

    在这之前会话路径**一步都不记**，于是 ``agent_steps`` 那道闸门在会话里形同虚设：
    同一个 Agent 走阶段作业会被限步，走聊天不受限，而没有任何地方看得出来。
    """
    project, conversation, _, _, chat = _setup(session, "pending", "pending")
    gateway = ScriptedGateway([reply("第一步做完了。"), reply("第二步也做完了。")])
    outcome = _loop(gateway).run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
    )

    assert outcome.status == "succeeded", outcome.reason
    assert outcome.rounds == 2 and outcome.steps_done == 2

    # ① 步数真的推进了 ``agent_runs.steps`` —— 预算闸门读的就是它
    assert runs_dao.get_run(session, chat.spec.run_id).steps == 2
    steps = runs_dao.list_steps(session, chat.spec.run_id)
    assert [s.kind for s in steps] == ["kernel_round", "kernel_round"]
    assert [s.content["round"] for s in steps] == [1, 2]

    # ② 检查点：起始一条 + 每轮一条 + 收尾一条，且状态依次推进
    rows = _rows(session, conversation.id)
    assert [r.status for r in rows] == [
        "running", "running", "running", "done",
    ]
    assert [r.step_index for r in rows] == [1, 2, 2, 2], "序号单调不减，收尾时停在最后一步"
    assert rows[-1].snapshot["steps_done"] == 2
    assert rows[-1].snapshot["next_step_id"] is None


def test_a_cancel_stops_at_the_safe_point_and_stays_resumable(session):
    """中止 = **暂停**，不是失败。落 failed 的话用户唯一的出路是重来，做过的那几步白做。"""
    project, conversation, plan, job, chat = _setup(session, "pending", "pending")

    def press_stop(_session):
        jobs_dao.request_cancel(_session, job.id)

    gateway = ScriptedGateway([reply("第一步做完了。")], after_first=press_stop)
    outcome = _loop(gateway).run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
    )

    assert outcome.status == "paused", outcome
    assert "中止" in outcome.reason
    # **只跑了一轮**：中止在下一轮的安全点生效，没有把第 2 步也做掉
    assert outcome.rounds == 1 and outcome.steps_done == 1
    assert len(gateway.calls) == 1, "安全点读过标记之后就不该再去问模型"

    # run 是 paused（不是 failed），计划回到 approved 而不是停在 executing ——
    # 留在 executing 会让重规划与续跑都被 ``has_running_plan`` 永远挡住
    assert runs_dao.get_run(session, chat.spec.run_id).status == "paused"
    assert task_plans_dao.get(session, plan.id).status == "approved"

    rows = _rows(session, conversation.id)
    assert rows[-1].status == "cancelled"
    assert rows[-1].snapshot["next_step_id"] == S2, "续跑点必须是没做完的那一步"
    assert rows[-1].snapshot["reason"]
    # 没有审批单：中止是人的指令，没有什么要再批一遍的
    assert rows[-1].snapshot.get("approval_id") is None

    # 此刻问「能不能续」应当答「能，从 s2」
    decision = kernel_store.resume_decision(session, conversation.id)
    assert decision.resumable and decision.start_step_id == S2


def test_resuming_continues_the_sequence_and_does_not_redo_finished_steps(session):
    """G2 第 5 条：**续跑接着数，且不重做**。

    两条断言各自独立地重要：只断「跑完了」的话，一份「从头再跑一遍」的实现也能通过 ——
    而那正是这条需求要挡的东西。
    """
    project, conversation, plan, job, chat = _setup(session, "pending", "pending")

    def press_stop(_session):
        jobs_dao.request_cancel(_session, job.id)

    _loop(ScriptedGateway([reply("第一步做完了。")], after_first=press_stop)).run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
    )
    before = _rows(session, conversation.id)
    assert [r.step_index for r in before] == [1, 2, 2]
    first_run_id = chat.spec.run_id

    # ── 续跑：新开一次运行（也就新开一行作业），从检查点指的那一步接着跑 ──
    decision = kernel_store.resume_decision(session, conversation.id)
    resumed = kernel_store.open_chat_run(
        session, project_id=project.id, conversation_id=conversation.id,
        job_id=_new_job(session, project.id, conversation.id).id,
    )
    gateway = ScriptedGateway([reply("第二步也做完了。")])
    outcome = _loop(gateway).run(
        session, run=resumed.spec, store=resumed.store, spec=resumed.agent_spec,
        resume=decision,
    )

    assert outcome.status == "succeeded", outcome.reason
    assert outcome.rounds == 1, "第 1 步已经做完了，不该再花一轮"
    assert outcome.reason and "续跑" in outcome.reason

    # ① 第二步真做了、第一步没被重做
    assert [s.content["step_id"] for s in runs_dao.list_steps(session, resumed.spec.run_id)] == [S2]
    assert [s.content["step_id"] for s in runs_dao.list_steps(session, first_run_id)] == [S1]
    statuses = [s.status for s in planner.Plan.from_row(task_plans_dao.get(session, plan.id)).steps]
    assert statuses == ["done", "done"], statuses

    # ② **序号连续**：跨两次运行连起来看是 1,2,2 → 2,2,2，既不重数也不倒退
    after = _rows(session, conversation.id)
    assert [r.status for r in after] == [
        "running", "running", "cancelled",   # 第一次运行：起始 + 第 1 轮 + 中止
        "running", "running", "done",        # 第二次运行：起始 + 第 1 轮 + 收尾
    ]
    indexes = [r.step_index for r in after]
    assert indexes == [1, 2, 2, 2, 2, 2], indexes
    assert indexes == sorted(indexes), "序号必须单调不减"


def test_resume_marks_the_run_as_a_resume_in_the_event_stream(session):
    """不标注的话，一次续跑与一次全新运行在界面上长得一模一样 ——
    用户会以为系统自己把同一批步骤又跑了一遍。"""
    project, conversation, plan, job, chat = _setup(session, "pending")
    decision = kernel_checkpoints.ResumeDecision(
        True, "从步骤 's1' 继续", start_step_id=S1, step_index=1, rounds=1,
    )
    _loop(ScriptedGateway([reply("做完了。")])).run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec, resume=decision,
    )

    starts = [
        e.payload for e in jobs_dao.events_after(session, job.id)
        if e.type == "stage.start"
    ]
    assert starts and starts[0]["resumed"] is True
    assert "s1" in starts[0]["resume_reason"]


def test_budget_exhaustion_pauses_and_can_be_resumed(session):
    """G2 第 4 条。走**真的** ``BudgetManager``：闸门读的是 ``agent_runs.steps``，
    而它只由内核的 ``mark_step`` 推进 —— 把这一步写成假件，就等于没验。"""
    project, conversation, plan, job, chat = _setup(session, "pending", "pending")
    agents_dao.upsert(
        session, agent_id="kernel", name="会话内核", tier="plan", budget_steps=1,
    )
    session.commit()

    gateway = ScriptedGateway(
        [reply("第一步做完了。"), reply("第二步做完了。")],
        budget=BudgetManager({"project_total": 50.0, "project_daily": 10.0}),
        run_coords=(project.id, "kernel", chat.spec.run_id),
    )
    outcome = _loop(gateway).run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
    )

    assert outcome.status == "paused"
    assert "agent_steps" in outcome.reason, outcome.reason
    assert len(gateway.calls) == 1, "熔断发生在第 2 轮之前，模型不该被问第二次"

    rows = _rows(session, conversation.id)
    assert rows[-1].status == "paused"
    assert rows[-1].snapshot["reason"] == "agent_steps"
    assert runs_dao.get_run(session, chat.spec.run_id).status == "paused"
    assert task_plans_dao.get(session, plan.id).status == "approved", "计划要能再次被执行"

    decision = kernel_store.resume_decision(session, conversation.id)
    assert decision.resumable and decision.start_step_id == S2

    # 批准追加额度之后续跑能收口
    grants_dao.create(session, project_id=project.id, scope="agent_steps",
                      amount=20, agent_id="kernel")
    session.commit()
    resumed = kernel_store.open_chat_run(
        session, project_id=project.id, conversation_id=conversation.id,
        job_id=_new_job(session, project.id, conversation.id).id,
    )
    gateway.responses = [reply("第二步做完了。")]
    gateway.run_coords = (project.id, "kernel", resumed.spec.run_id)
    outcome = _loop(gateway).run(
        session, run=resumed.spec, store=resumed.store, spec=resumed.agent_spec,
        resume=decision,
    )
    assert outcome.status == "succeeded", outcome.reason
    statuses = [s.status for s in planner.Plan.from_row(task_plans_dao.get(session, plan.id)).steps]
    assert statuses == ["done", "done"], statuses


def test_a_cancel_that_arrives_before_the_first_round_wastes_nothing(session):
    """停止标记在第一轮之前就在：一轮都不该跑。

    安全点与 ``max_steps`` 的检查同在循环开头，**顺序是有讲究的**：既然要停就先停，
    而不是先报一个「步数超限」的失败 —— 用户按了停止，收到的却是一条失败，
    会以为是自己把会话跑坏了。
    """
    project, conversation, plan, job, chat = _setup(session, "pending", "pending")
    jobs_dao.request_cancel(session, job.id)  # 开跑之前就被按了停止

    gateway = ScriptedGateway([reply("不该被问到。")])
    outcome = _loop(gateway).run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
    )

    assert outcome.status == "paused"
    assert outcome.rounds == 0 and gateway.calls == [], "一轮都不该跑"
    rows = _rows(session, conversation.id)
    assert [r.status for r in rows] == ["running", "cancelled"]
    assert rows[-1].step_index == 1, "续跑点仍是最开始那一步"
    assert kernel_store.resume_decision(session, conversation.id).resumable is True


def test_the_cancel_mailbox_is_read_across_sessions(session, session_factory):
    """中止标记必须**跨 session** 读得到 —— 生产里它就是那么读的。

    ``JobRunner.request_stop`` 在 HTTP 线程里用另一个 session 写，循环在工作线程里用
    自己那个 session 读。而 ``session.get(Job, …)`` 会先命中身份映射，本项目所有
    session 又都是 ``expire_on_commit=False``（提交后对象不失效）—— 于是「同一个 id 的
    那个旧对象」把新值挡在外面，循环永远读到当初那份 ``cancel_requested=False``。
    表现是：用户按了停止，界面一直显示「正在停止」，而作业自己一路跑到底。

    这一条要用一个**已经加载过该作业的** session 去读，才能把那条路钉死 ——
    在此之前的所有用例都在同一个 session 里写和读，恰好从这条路的旁边绕了过去
    （US-407 收尾时真机上才暴露出来）。
    """
    project, conversation, plan, job, chat = _setup(session, "pending", "pending")
    # 先让循环那个 session 的身份映射里**装进**这个作业行：这一步是复现的关键
    assert jobs_dao.get(session, job.id).cancel_requested is False

    with session_factory() as other:
        jobs_dao.request_cancel(other, job.id)

    assert jobs_dao.cancel_requested(session, job.id) is True, (
        "另一个 session 写下的标记必须能读到；读到 False 说明它命中了身份映射里的旧对象"
    )


def test_a_stop_from_another_session_actually_halts_the_loop(session, session_factory):
    """上一条的循环侧版本：**别人**按的停止，循环必须真的停。

    钩子刻意用 ``session_factory()`` 另开一条 session 去写标记（而不是像其它用例那样
    拿循环自己的 session）—— 那正是 HTTP 线程与工作线程的关系。
    """
    project, conversation, plan, job, chat = _setup(session, "pending", "pending")
    assert jobs_dao.get(session, job.id) is not None  # 循环的 session 已经认识这个作业

    def stop_it(_session) -> None:
        with session_factory() as other:
            jobs_dao.request_cancel(other, job.id)

    gateway = ScriptedGateway([reply("第一步做完了。")], after_first=stop_it)
    outcome = _loop(gateway, session_factory=session_factory).run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
    )

    assert outcome.status == "paused", outcome.reason
    assert "中止" in outcome.reason, outcome.reason
    assert len(gateway.calls) == 1, "第 1 步收口后就该停，不该追问模型第二轮"
    assert _rows(session, conversation.id)[-1].status == "cancelled"
    assert kernel_store.resume_decision(session, conversation.id).start_step_id == S2


# ── 三、HTTP：续跑入口 ──────────────────────────

@pytest.fixture
def client(engine, session_factory):
    app = create_app()
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app) as c:
        yield c


def _reopened(session_factory):
    """开一条**新连接**去读作业写下的东西。

    会话夹具的 ``sessionmaker`` 是 ``expire_on_commit=False``：同一个 session 里的对象
    会一直保持第一次读到的值。于是「作业真的把结果写回去了吗」这个问题在本会话里
    **答不出来** —— 读到的是一份缓存的旧快照，而不是「写失败了」。
    跨连接写就必须跨连接验，否则会得到一条指向完全错误方向的失败。
    """
    return session_factory()


def test_resume_is_refused_with_a_reason_when_there_is_nothing_to_resume(client, session):
    """不能续就把话说清楚（409），而不是受理之后让作业转几圈再挂掉 ——
    那时原因只藏在作业事件里，用户得自己翻。"""
    project = projects_dao.create(session, title="空会话", goal="")
    conversation = conversations_dao.create(session, project_id=project.id, title="还没跑过")
    session.commit()

    resp = client.post(f"/api/conversations/{conversation.id}/resume")
    assert resp.status_code == 409, resp.text
    assert "没有任何检查点" in resp.json()["detail"]


def test_full_chain_cancel_then_resume_over_http(client, session, session_factory):
    """端到端：中止 → 检查点 → 续跑 → 计划收口。

    跨「内核 → 数据库 → HTTP → 作业通道」四层不打桩。每一层各自绿、连起来不通，
    是这类功能最常见的失败形态。
    """
    app = client.app
    project = projects_dao.create(session, title="端到端", goal="把两步做完")
    conversation = conversations_dao.create(session, project_id=project.id, title="跑两步")
    session.flush()
    payload = [
        planner.PlanStep(id=S1, title="第 1 步").to_dict(),
        planner.PlanStep(id=S2, title="第 2 步").to_dict(),
    ]
    plan = task_plans_dao.create(
        session, conversation_id=conversation.id, steps=payload,
        mode=planner.PLAN_EXECUTE, deterministic=True, seed=0,
        title="两步计划", status="approved",
    )
    job = jobs_dao.create(
        session, project_id=project.id, kind="chat",
        params={"conversation_id": conversation.id},
    )
    session.commit()

    # ① 跑一次，跑完第 1 步就中止
    app.state.kernel_loop.gateway = ScriptedGateway(
        [reply("第一步做完了。")],
        after_first=lambda s: jobs_dao.request_cancel(s, job.id),
    )
    chat = kernel_store.open_chat_run(
        session, project_id=project.id, conversation_id=conversation.id, job_id=job.id,
    )
    outcome = app.state.kernel_loop.run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
    )
    assert outcome.status == "paused" and "中止" in outcome.reason

    # ② 续跑：走 HTTP，不追加任何用户消息
    app.state.kernel_loop.gateway = ScriptedGateway([reply("第二步也做完了。")])
    resp = client.post(f"/api/conversations/{conversation.id}/resume")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["resume_from"] == S2, body
    assert body["reason"]

    done = _await(client, body["job_id"])
    assert done["status"] == "succeeded", done

    with _reopened(session_factory) as fresh:
        steps = [
            s.status
            for s in planner.Plan.from_row(task_plans_dao.get(fresh, plan.id)).steps
        ]
        assert steps == ["done", "done"], steps

        rows = _rows(fresh, conversation.id)
        assert rows[0].status == "running" and rows[-1].status == "done"
        indexes = [r.step_index for r in rows]
        assert indexes == sorted(indexes) and indexes[-1] == 2, indexes

    # ③ 已经跑完的会话再点「继续」= 重跑，要拒绝（而不是把两步再做一遍）
    again = client.post(f"/api/conversations/{conversation.id}/resume")
    assert again.status_code == 409, again.text
    assert "已经跑完" in again.json()["detail"]


def test_cancelling_a_running_chat_job_is_accepted_asynchronously(
    client, session, session_factory,
):
    """运行中的会话作业：受理中止请求（202），**不谎称已经停了**。

    线程还在跑，此刻就落终态会留下「线程还在写、台账已判死」的错乱状态。
    """
    project = projects_dao.create(session, title="中止", goal="")
    conversation = conversations_dao.create(session, project_id=project.id, title="中止")
    session.flush()
    job = jobs_dao.create(
        session, project_id=project.id, kind="chat",
        params={"conversation_id": conversation.id},
    )
    jobs_dao.mark_running(session, job.id)
    session.commit()

    resp = client.post(f"/api/jobs/{job.id}/cancel")
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"job_id": job.id, "status": "stopping", "stop_requested": True}
    assert jobs_dao.is_terminal(jobs_dao.get(session, job.id)) is False, (
        "202 说的是「已受理」，不是「已结束」—— 此刻就落终态会留下台账与线程不一致的状态"
    )

    # 标记写在另一条连接上，必须跨连接验（见 ``_reopened``）
    with _reopened(session_factory) as fresh:
        assert jobs_dao.cancel_requested(fresh, job.id) is True
        assert jobs_dao.get(fresh, job.id).status == "running", "状态要等循环自己收尾"


def test_cancelling_a_running_stage_job_is_still_refused(client, session):
    """阶段作业没有安全点，置了标记也没人看 —— 受理一个永远不会生效的请求，
    比直接拒绝更坏：用户以为停了，它其实还在跑。"""
    project = projects_dao.create(session, title="阶段作业", goal="")
    session.flush()
    job = jobs_dao.create(session, project_id=project.id, kind="stage", stage_id="S1")
    jobs_dao.mark_running(session, job.id)
    session.commit()

    resp = client.post(f"/api/jobs/{job.id}/cancel")
    assert resp.status_code == 409, resp.text


def test_a_cancelled_job_pauses_with_a_reason_that_says_cancelled(
    client, session, session_factory,
):
    """用户按了停止之后，界面必须看到「已中止」，而不是「不知为何暂停」。

    ``job.paused`` 的 ``reason`` 是界面唯一能拿到的解释，它由 ``JobRunner._pause_reason``
    反推：先看该 run 名下有没有待批单，没有再看作业自己的取消标记。
    **中止没有待批单** —— 所以这条链少一环，界面就只剩 ``unknown``，
    而用户面对一个「不知为何停住的会话」时，能做的只有重来。

    ⚠️ 按停止的那一方必须**另开一条 session**（``session_factory()``），与 HTTP 线程
    写标记是同一种关系。用循环自己那条 session 写会命中同一个身份映射中的对象，
    于是把「读不到别人写的标记」这个真问题盖住 —— US-407 收尾时正是这么漏掉的。
    """
    app = client.app
    project = projects_dao.create(session, title="中止原因", goal="两步")
    conversation = conversations_dao.create(session, project_id=project.id, title="两步")
    session.flush()
    task_plans_dao.create(
        session, conversation_id=conversation.id,
        steps=[planner.PlanStep(id=S1, title="第 1 步").to_dict(),
               planner.PlanStep(id=S2, title="第 2 步").to_dict()],
        mode=planner.PLAN_EXECUTE, deterministic=True, seed=0,
        title="两步计划", status="approved",
    )
    session.commit()

    def press_stop(_session):
        # 作业是 worker 起的，这里只知道「此刻在跑哪一个」；
        # 写标记的 session 刻意与循环那条分开（见 docstring）
        with session_factory() as other:
            jobs_dao.request_cancel(other, app.state.job_runner.current_job_id())

    app.state.kernel_loop.gateway = ScriptedGateway(
        [reply("第一步做完了。")], after_first=press_stop,
    )
    job = app.state.job_runner.submit(
        session, project_id=project.id, kind="chat",
        params={"conversation_id": conversation.id},
    )
    session.commit()

    snapshot = _await(client, job.id)
    assert snapshot["status"] == "paused", snapshot
    with _reopened(session_factory) as fresh:
        event = jobs_dao.last_event(fresh, job.id)
        assert event.type == "job.paused", event.type
        assert event.payload["reason"] == "cancelled", event.payload
        # 中止不开审批单：它是人的指令，没有什么要再批一遍的
        assert approvals_dao.list_by_status(fresh, project.id, "pending") == []


def _await(client, job_id: int, timeout: float = 30.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        snapshot = resp.json()
        if snapshot["status"] in ("succeeded", "failed", "paused"):
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"作业 {job_id} 未在 {timeout}s 内结束：{snapshot}")


def test_the_budget_error_code_is_still_a_pause_signal():
    """``BudgetExceeded`` 是暂停信号而不是失败。它的码随 ``str(exc)`` 一路进事件与
    ``agent_runs.error``，所以钉住它 —— 改了这个字符串，线上的排查手册就失效了。"""
    assert BudgetExceeded("agent_steps", {}).code == "LLM-BUDGET-001"
