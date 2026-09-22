"""危险操作的审批接线（US-406，对齐设计 §10.1 与决策 D4）。

这这份测试要证明的是一条**证据链**，而不是某个函数对不对：

    模型要求执行一条命令  →  内核整轮停住、一个副作用都没产生
                          →  库里多出一张待批单（kind=dangerous）
                          →  人批准
                          →  那条命令**真的执行了**，审计里能查到它是谁批的

G2 第 3 条（「危险操作可批准」）要的正是这条链，所以这里刻意让它跨过
「内存 store → 真实数据库 → HTTP 接口 → 作业通道」四层，中间不打桩 ——
每一层各自绿、连起来不通，是这类功能最常见的失败形态。

反过来的一半同样重要：**没有副作用**必须是可断言的。挂起时若已经跑掉几个调用，
「先跑了再问」就成了事实，而审批只剩一个仪式。
"""

from __future__ import annotations

#: 真正会被执行的那条命令：``python -c "print('done')"``。
#: 用 ``sys.executable`` 而不是 ``python`` —— 沙箱只透传 PATH，用绝对路径最稳。
import sys  # noqa: E402
import time

import pytest
from fastapi.testclient import TestClient

from app.agent_kernel.loop import KernelLoop
from app.agent_kernel.permissions import PendingCall, grant_key
from app.agent_kernel.sandbox import SandboxPolicy
from app.agent_kernel.tools.factory import build_registry
from app.ai.base import ChatResponse, ToolCall
from app.jobs.events import APPROVAL_REQUIRED, STAGE_PAUSED
from app.main import create_app
from app.orchestration import kernel_store
from app.store.dao import app_config as app_config_dao
from app.store.dao import approvals as approvals_dao
from app.store.dao import conversations as conversations_dao
from app.store.dao import decisions as decisions_dao
from app.store.dao import jobs as jobs_dao
from app.store.dao import messages as messages_dao
from app.store.dao import projects as projects_dao
from app.store.dao import runs as runs_dao
from app.store.dao import tool_calls as tool_calls_dao

PRINT_DONE = {"argv": [sys.executable, "-c", "print('done')"], "timeout_s": 20}


class ScriptedGateway:
    """按脚本返回响应。脚本用完还问 = 循环没收敛，直接失败。"""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def call(self, session, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("on_start") is not None:
            kwargs["on_start"](kwargs["tier"], [{"provider": "fake", "model": "m"}])
        assert self.responses, "模型被多问了一轮（循环没有收敛）"
        return self.responses.pop(0)


def reply(text: str = "", calls=()) -> ChatResponse:
    return ChatResponse(
        text=text, provider="fake", model="m",
        tool_calls=[ToolCall(id=c[0], name=c[1], arguments=c[2]) for c in calls] or None,
    )


@pytest.fixture
def sandbox_root(tmp_path):
    return tmp_path / "workspace"


def _loop(gateway, sandbox_root, *, session_factory=None) -> KernelLoop:
    policy = SandboxPolicy.from_config(sandbox_root, {"timeout_s": 20})
    return KernelLoop(
        gateway=gateway,
        tools=build_registry(
            runner=lambda *a, **k: [], stage_ids=["S1"], policy=policy,
        ),
        session_factory=session_factory,
    )


def _seed_file(sandbox_root, project_id: int, rel: str, content: str):
    """往项目沙箱里放一个文件。

    直接落盘而不是走 ``write_file`` 工具：这个文件是**测试的前提**（被读的东西
    得先存在），用工具去铺前提会把「写」也拉进被测路径，一个失败的写会让
    读的断言指向错误的方向。
    """
    policy = SandboxPolicy.from_config(sandbox_root, {"timeout_s": 20})
    target = policy.ensure_project_dir(project_id) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _project_and_conversation(session):
    project = projects_dao.create(session, title="审批流", goal="把实验跑一遍")
    conversation = conversations_dao.create(session, project_id=project.id, title="跑一遍")
    session.commit()
    return project, conversation


# ── 一、挂起：整轮停住，零副作用 ────────────────

def test_dangerous_call_suspends_the_round_without_any_side_effect(session, sandbox_root):
    """模型同时要跑命令**和**读文件：整轮挂起，两条都不执行。

    为什么不是「先跑只读的、把危险的留下」：assistant 那条消息里的 ``tool_calls``
    是一个整体，只回填一半会让下一轮请求出现「有调用没有结果」的配对，端点直接拒收。
    """
    project, conversation = _project_and_conversation(session)
    chat = kernel_store.open_chat_run(
        session, project_id=project.id, conversation_id=conversation.id,
        agent_id="executor",
    )
    gateway = ScriptedGateway([reply(calls=[
        ("c1", "run_command", _json(PRINT_DONE)),
        ("c2", "read_file", _json({"path": "notes.md"})),
    ])])
    outcome = _loop(gateway, sandbox_root).run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
    )

    assert outcome.status == "paused"
    assert outcome.reason and "危险操作" in outcome.reason

    # ① run 是 paused —— 不是 failed。「跑挂了」会让用户的第一反应变成重试，
    #    于是又开一张审批单。
    assert runs_dao.get_run(session, chat.spec.run_id).status == "paused"

    # ② 一张待批单，kind=dangerous（D4：复用 approvals 表）
    pending = approvals_dao.list_by_status(session, project.id, "pending")
    assert len(pending) == 1, "一轮只该开一张单子"
    approval = pending[0]
    assert approval.kind == "dangerous" and approval.run_id == chat.spec.run_id
    detail = approval.detail
    assert [c["tool"] for c in detail["pending"]] == ["run_command"]
    assert {c["call_id"] for c in detail["round"]} == {"c1", "c2"}, "整轮都在，恢复时要重放"
    assert detail["conversation_id"] == conversation.id

    # ③ **零副作用**：一条工具调用记录都没有，一条工具消息都没有。
    #    两条都断 —— 只断其一的话，「跑了但没记账」和「没跑」会分不清。
    assert tool_calls_dao.count_for_conversation(session, conversation.id) == 0
    roles = [m.role for m in messages_dao.list_for_conversation(session, conversation.id)]
    assert roles == ["assistant"], "只该留下那条带 tool_calls 的助手消息"

    # ④ assistant 那条消息必须**带** tool_calls：恢复时工具结果要能对回它
    assistant = messages_dao.list_for_conversation(session, conversation.id)[0]
    assert [c["id"] for c in assistant.tool_calls] == ["c1", "c2"]


def test_the_approval_event_is_emitted_so_the_ui_can_stop_waiting(session, sandbox_root):
    """没有这条事件，界面上「等待批准」与「模型正在思考」长得一模一样。

    事件带上 ``approval_id`` 与待批清单：不带的话前端只能按 ``(run_id, kind)``
    反查审批单，而同一个 run 前后可能暂停过不止一次。
    """
    project, conversation = _project_and_conversation(session)
    chat = kernel_store.open_chat_run(
        session, project_id=project.id, conversation_id=conversation.id,
        agent_id="executor", job_id=_a_job(session, project.id),
    )
    gateway = ScriptedGateway([reply(calls=[("c1", "run_command", _json(PRINT_DONE))])])
    _loop(gateway, sandbox_root).run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
    )

    events = jobs_dao.events_after(session, chat.spec.job_id)
    types = [e.type for e in events]
    assert STAGE_PAUSED in types and APPROVAL_REQUIRED in types
    # 暂停事件必须先于「待批准」到达：顺序反过来的话，界面会先弹出一张
    # 指向尚未存在的审批单的卡片。
    assert types.index(STAGE_PAUSED) < types.index(APPROVAL_REQUIRED)

    payload = next(e.payload for e in events if e.type == APPROVAL_REQUIRED)
    assert payload["approval_id"] == approvals_dao.list_by_status(
        session, project.id, "pending")[0].id
    assert payload["pending"][0]["tool"] == "run_command"


def _a_job(session, project_id: int) -> int:
    """事件是挂在作业上的。这里造一行作业来承载，好让事件真的落库。"""
    job = jobs_dao.create(session, project_id=project_id, kind="chat", params={})
    session.commit()
    return job.id


# ── 二、批准后重放 ──────────────────────────────

def test_execute_approved_replays_the_whole_round_and_marks_who_approved_it(
    session, sandbox_root,
):
    """恢复 = **重放这一轮**，不是重跑这个作业。

    人批准的是「这一次调用」；重跑作业会重新问一次模型，而它很可能给出另一组调用 ——
    那样批准的对象就悄悄换了。所以计划与消息都不重新生成，只补上缺的那几条结果。
    """
    project, conversation = _project_and_conversation(session)
    # 读文件那条要有东西可读：让「整轮重放」里的只读调用也真的成功，
    # 否则「搭车的调用没被执行」与「执行了但失败」会分不清。
    _seed_file(sandbox_root, project.id, "notes.md", "实验记录")
    chat = kernel_store.open_chat_run(
        session, project_id=project.id, conversation_id=conversation.id,
        agent_id="executor",
    )
    gateway = ScriptedGateway([
        reply(calls=[
            ("c1", "run_command", _json(PRINT_DONE)),
            ("c2", "read_file", _json({"path": "notes.md"})),
        ]),
        reply(text="命令跑完了。"),
    ])
    loop = _loop(gateway, sandbox_root)
    loop.run(session, run=chat.spec, store=chat.store, spec=chat.agent_spec)

    approval = approvals_dao.list_by_status(session, project.id, "pending")[0]
    round_calls = tuple(PendingCall.from_dict(c) for c in approval.detail["round"])
    approvals_dao.decide(session, approval.id, "approved")
    session.commit()

    executed = loop.execute_approved(
        session, run=chat.spec, store=chat.store, round_calls=round_calls,
        spec=chat.agent_spec, approval_id=approval.id,
    )
    session.commit()

    assert [item.call.name for item in executed] == ["run_command", "read_file"]
    assert all(item.status == "ok" for item in executed), [i.error for i in executed]

    rows = tool_calls_dao.list_for_conversation(session, conversation.id)
    assert [r.tool_name for r in rows] == ["run_command", "read_file"]
    # 只有真正经过人工放行的那条挂审批单号：同一轮搭车的只读调用被记成「人批的」
    # 会让审批单看起来批了更多东西。
    assert rows[0].approval_id == approval.id
    assert rows[1].approval_id is None

    # 工具结果按调用序回填，且每条都对得回它的调用
    messages = messages_dao.list_for_conversation(session, conversation.id)
    assert [m.role for m in messages] == ["assistant", "tool", "tool"]
    assert [m.tool_call_id for m in messages[1:]] == ["c1", "c2"]
    assert "done" in messages[1].content

    # 补完之后循环能继续：模型看到结果，给出结论
    outcome = loop.run(session, run=chat.spec, store=chat.store, spec=chat.agent_spec)
    assert outcome.status == "succeeded"
    final = messages_dao.list_for_conversation(session, conversation.id)[-1]
    assert final.role == "assistant" and "跑完了" in final.content


def test_approval_required_is_not_recorded_as_a_failure(session, sandbox_root):
    """挂起不是失败：``agent_runs`` 与决策日志里都不该出现 failed_attempt。"""
    project, conversation = _project_and_conversation(session)
    chat = kernel_store.open_chat_run(
        session, project_id=project.id, conversation_id=conversation.id,
        agent_id="executor",
    )
    gateway = ScriptedGateway([reply(calls=[("c1", "run_command", _json(PRINT_DONE))])])
    _loop(gateway, sandbox_root).run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
    )

    run = runs_dao.get_run(session, chat.spec.run_id)
    assert run.status == "paused" and not run.error
    kinds = [d.kind for d in decisions_dao.list_for_project(session, project.id)]
    assert "failed_attempt" not in kinds


def test_a_tool_outside_the_whitelist_is_refused_without_asking_a_human(session, sandbox_root):
    """白名单外的调用：**直接拒，不弹批准卡**。

    它属于「注定被拒」那一类（``_gate`` 里已经承认了未注册 / 参数坏两种）：
    执行期本来就会把它拒掉，先要人批准一次等于让人批准一件不会发生的事。
    批准卡里一旦混进「批了也没用」的项，人就开始不看内容地点同意 ——
    审批疲劳就是这么进来的，而它恰好是 D5「dangerous 不留记忆」要防的那个东西的源头。

    场景用会话内核自己的契约（``CONVERSATION_SPEC`` 不含沙箱工具）去发起
    ``run_command``：这正是「模型幻觉出一个它无权用的工具」。
    """
    project, conversation = _project_and_conversation(session)
    chat = kernel_store.open_chat_run(
        session, project_id=project.id, conversation_id=conversation.id,
    )
    assert "run_command" not in chat.agent_spec.tools, (
        "前提变了：会话内核拿到了沙箱工具，这条测试问的就不再是白名单"
    )

    gateway = ScriptedGateway([
        reply(calls=[("c1", "run_command", _json(PRINT_DONE))]),
        reply(text="那我换个办法。"),  # 拿到拒绝理由之后模型换路并收敛
    ])
    outcome = _loop(gateway, sandbox_root).run(
        session, run=chat.spec, store=chat.store, spec=chat.agent_spec,
    )

    # **核心断言**：一张审批单都不该开，作业正常收敛而不是挂起
    assert approvals_dao.list_by_status(session, project.id, "pending") == []
    assert outcome.status == "succeeded", outcome.reason
    assert (outcome.tool_calls, outcome.rejected) == (1, 1), outcome
    assert runs_dao.get_run(session, chat.spec.run_id).status == "succeeded"

    # 拒绝的理由要真的落到审计与回填的消息里 —— 模型只有拿到理由才能换个办法
    records = tool_calls_dao.list_for_run(session, chat.spec.run_id)
    assert [r.status for r in records] == ["rejected"], records
    assert "白名单" in (records[0].error or ""), records[0].error
    roles = [m.role for m in messages_dao.list_for_conversation(session, conversation.id)]
    assert roles == ["assistant", "tool", "assistant"], roles


# ── 三、HTTP：批准之后的记忆与审计 ──────────────

@pytest.fixture
def client(engine, session_factory):
    app = create_app()
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app) as c:
        yield c


def _crafted_approval(session, *, tool: str, permission: str, grant_key_value, call_id="c9"):
    """手工造一张待批单，用来单独验「批准之后签了什么、记了什么」。

    为什么不都走真流程：唯一真实的 ``execute`` 工具是 ``run_pipeline``，
    批准它就得让恢复真的去跑一个阶段 —— 那条链由冒烟脚本覆盖，
    这里要钉的是**签发规则本身**（``execute`` 签记忆、``dangerous`` 不签），
    用手工单能把两个分支各断一次，且不受阶段执行的影响。
    """
    project, conversation = _project_and_conversation(session)
    approval = approvals_dao.create(
        session, project_id=project.id, kind="dangerous", detail={
            "source": "tool_permission", "stage_id": "chat", "agent_id": "executor",
            "conversation_id": conversation.id, "reason": "测试用",
            "pending": [{
                "call_id": call_id, "tool": tool, "args": {},
                "permission": permission, "reason": "测试用",
                "grant_key": grant_key_value, "needs_approval": True,
            }],
            "round": [{
                "call_id": call_id, "tool": tool, "args": {},
                "permission": permission, "reason": "测试用",
                "grant_key": grant_key_value, "needs_approval": True,
            }],
        },
    )
    session.commit()
    return project, conversation, approval


def test_approving_an_execute_call_signs_a_memory_key(client, session):
    """``execute`` 档：批准一次 → 签记忆 → 下次不再要求批准（D5 的「按作用域记忆」）。"""
    project, _, approval = _crafted_approval(
        session, tool="run_shell", permission="execute",
        grant_key_value=grant_key("run_shell"),
    )
    resp = client.post(f"/api/approvals/{approval.id}/approve", json={"note": "可以跑"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["granted"] == [grant_key("run_shell")]

    stored = app_config_dao.get(session, grant_key("run_shell"))
    assert stored is not None and stored["tool"] == "run_shell"
    assert stored["approval_id"] == approval.id

    reasons = [d.reason for d in decisions_dao.list_for_project(session, project.id)]
    assert "可以跑" in reasons


def test_approving_a_dangerous_call_signs_nothing(client, session):
    """**本文件里最重要的一条否向断言**：``dangerous`` 不留任何记忆。

    留了就是「一次批准换永久放行」—— 而审批疲劳正是绕过的开始。
    """
    _, _, approval = _crafted_approval(
        session, tool="run_command", permission="dangerous", grant_key_value=None,
    )
    body = client.post(f"/api/approvals/{approval.id}/approve", json={}).json()
    assert "granted" not in body
    assert app_config_dao.get_prefix(session, "tool_grant:") == {}


def test_rejecting_signs_nothing_and_does_not_resume(client, session):
    """拒绝就是拒绝：不签记忆、不派作业。顺带把「已批准」也做了是最糟的一种。"""
    _, _, approval = _crafted_approval(
        session, tool="run_shell", permission="execute",
        grant_key_value=grant_key("run_shell"),
    )
    body = client.post(f"/api/approvals/{approval.id}/reject", json={"note": "先别跑"}).json()
    assert body["status"] == "rejected"
    assert "job_id" not in body and "granted" not in body
    assert app_config_dao.get_prefix(session, "tool_grant:") == {}


def test_approving_an_approval_that_does_not_exist_is_a_404(client):
    resp = client.post("/api/approvals/999999/approve", json={})
    assert resp.status_code == 404


# ── 四、端到端：批准 → 恢复 → 那条命令真的跑了 ──

def test_full_chain_from_suspension_to_a_real_execution(
    client, session, session_factory, engine, tmp_path,
):
    """G2 第 3 条的完整证据链，跨四层不打桩：

    提交会话作业 → 内核停住 → HTTP 查到待批单 → 批准 → 恢复作业 → 命令真的执行。
    """
    app = client.app
    # 换掉内核的模型接入：脚本化两次回答（先要命令、后给结论）。
    # 换的是**内核的那一个**，``app.state.gateway`` 保持原样给编排层用。
    app.state.kernel_loop.gateway = ScriptedGateway([
        reply(calls=[("c1", "run_command", _json(PRINT_DONE))]),
        reply(text="命令已执行完毕。"),
    ])

    project = projects_dao.create(session, title="端到端", goal="跑一条命令")
    conversation = conversations_dao.create(session, project_id=project.id, title="跑一条命令")
    session.commit()

    # ``agent_id=executor`` 是必须的：会话内核自己的白名单里**不含**沙箱工具，
    # 要动用它们得显式提权（§5.3「默认最严、提权要写明」）。这也是为什么
    # 这里直接派作业而不是走 POST /messages —— 后者不暴露 agent_id。
    job = app.state.job_runner.submit(
        session, project_id=project.id, kind="chat",
        params={"conversation_id": conversation.id, "agent_id": "executor"},
    )
    session.commit()

    paused = _await(client, job.id)
    assert paused["status"] == "paused", paused

    # 暂停原因写在 ``job.paused`` 事件里（作业快照本身不带 reason）。
    # 它必须是审批、不能是 budget —— 早先这里是写死的 "budget"，
    # 于是「等你批准执行这条命令」会在界面上显示成「预算熔断」：
    # 用户去加预算，而真正要做的是看一眼那条命令。
    pause_event = jobs_dao.last_event(session, job.id)
    assert pause_event.type == "job.paused"
    assert pause_event.payload["reason"] == "dangerous", pause_event.payload

    cards = client.get("/api/approvals", params={"project_id": project.id}).json()
    assert len(cards) == 1 and cards[0]["kind"] == "dangerous"
    approval_id = cards[0]["id"]
    assert cards[0]["detail"]["pending"][0]["tool"] == "run_command"

    approved = client.post(f"/api/approvals/{approval_id}/approve", json={"note": "跑吧"})
    assert approved.status_code == 200, approved.text
    resume_job_id = approved.json()["job_id"]

    done = _await(client, resume_job_id)
    assert done["status"] == "succeeded", done

    # 审计：那条命令真的执行了、退出码为 0、并且记着是谁批的
    rows = tool_calls_dao.list_for_conversation(session, conversation.id)
    assert [r.tool_name for r in rows] == ["run_command"]
    assert rows[0].status == "ok" and rows[0].approval_id == approval_id
    assert "done" in str(rows[0].result)

    # 恢复后循环继续跑完，模型给出了结论
    final = messages_dao.list_for_conversation(session, conversation.id)[-1]
    assert final.role == "assistant" and "执行完毕" in final.content

    # dangerous 没有留下任何记忆：下次还要再批一遍
    assert app_config_dao.get_prefix(session, "tool_grant:") == {}


def _await(client, job_id: int, timeout: float = 30.0) -> dict:
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


def _json(payload) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)
