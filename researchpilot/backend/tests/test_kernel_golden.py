"""deterministic 的 golden case（US-408，G2 第 7 条 / 设计 §12.5）。

§12.5 对 golden test 的要求是「**固定输入 + 固定断言**；``temperature=0``；
关键字段完全匹配」。这一份把它落到内核上，分四层：

1. **计划器**：``deterministic=True`` 时完全不问模型；同一目标两次的计划逐项相同。
2. **温度**：deterministic 下送给模型的是 ``0.0``，**且**非确定性下不是 —— 只断言
   前者的话，一个写死的 ``0.0`` 也能让测试变绿。
3. **端到端**：同一份脚本跑两个**互相独立的项目**（各自一份沙箱目录），
   工具调用序列、事件序列、**磁盘产物的 sha256** 三项逐项相等。
4. **产物可比的前提**：两次跑各自的沙箱互不影响 —— 否则第二次的 ``write_file``
   会看到「文件已存在」，两次的结果天生不同，比对就成了噪声。

这里刻意**不**包含 ``run_command``：它会触发危险操作审批（G2 第 3 条已由
``test_approval_flow.py`` 覆盖），掺进来会让「确定性」这个命题被审批状态干扰。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.agent_kernel import planner
from app.agent_kernel import specs as kernel_specs
from app.agent_kernel.loop import DEFAULT_TEMPERATURE, KernelLoop
from app.agent_kernel.sandbox import SandboxPolicy
from app.agent_kernel.tools.factory import build_registry
from app.ai.base import ChatResponse, ToolCall
from app.orchestration import kernel_store
from app.store.dao import conversations as conversations_dao
from app.store.dao import jobs as jobs_dao
from app.store.dao import messages as messages_dao
from app.store.dao import projects as projects_dao
from app.store.dao import task_plans as task_plans_dao
from app.store.dao import tool_calls as tool_calls_dao

GOAL = "跑通一次最小实验：写脚本 → 读回"

TOY = (
    "import json\n"
    "import pathlib\n"
    "\n"
    "out = pathlib.Path('experiments/toy_result.json')\n"
    "out.write_text(json.dumps({'ok': True, 'n': 42}), encoding='utf-8')\n"
)

#: 真实契约里唯一持有沙箱工具的那个 Agent（§5.3 权限最小化）。**显式传它**，
#: 而不是让循环去库里解析：`open_chat_run` 解析出来的会话内核白名单只有
#: `run_pipeline`，用它跑沙箱工具会全部被 `AGENT-TOOL-002` 拒掉 ——
#: 那正是生产里「提权要写明」的表现（走查脚本靠 `params.agent_id` 提权，这里靠显式 spec）。
EXECUTOR_SPEC = kernel_specs.by_agent_id("executor")


def reply(text: str = "", calls: tuple[tuple[str, dict], ...] = ()) -> ChatResponse:
    return ChatResponse(
        text=text, provider="fake", model="m",
        tool_calls=[
            ToolCall(id=f"c{index}", name=name,
                     arguments=json.dumps(args, ensure_ascii=False, sort_keys=True))
            for index, (name, args) in enumerate(calls, start=1)
        ] or None,
    )


def script() -> list[ChatResponse]:
    """固定输入：写脚本 → 读回 → 列目录 → 两步计划各自收口。

    ⚠️ 响应条数不是随手写的：``plan_execute`` 下**一步在「模型不再调工具」的那一轮
    才收口**（见 ``test_checkpoints`` 对 ``steps_done`` 的断言），所以 2 步计划
    需要 3 个工具轮 + 2 个文本轮。少一条，网关就会因为「脚本用完还被问」而失败 ——
    那正是这几份测试用来抓「循环没有收敛」的手法。
    """
    return [
        reply(calls=(("write_file", {"path": "experiments/toy.py", "content": TOY}),)),
        reply(calls=(("read_file", {"path": "experiments/toy.py"}),)),
        reply(calls=(("list_dir", {"path": "experiments"}),)),
        reply("脚本已落盘，产物已读回。"),
        reply("两步都完成了。"),
    ]


class ScriptedGateway:
    """按脚本回答，并把「第几次调用带了什么参数」记下来（温度断言要用）。"""

    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def call(self, session, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        on_start = kwargs.get("on_start")
        if on_start is not None:
            on_start(kwargs["tier"], [{"provider": "fake", "model": "m"}])
        assert self.responses, "模型被多问了一轮（说明这一轮没收敛）"
        return self.responses.pop(0)


def _loop(gateway, policy_root: Path) -> KernelLoop:
    registry = build_registry(
        runner=lambda *args, **kwargs: None,  # run_pipeline 本用例不调用
        stage_ids=("S1",),
        policy=SandboxPolicy.from_config(policy_root),
    )
    return KernelLoop(gateway=gateway, tools=registry, spec=EXECUTOR_SPEC)


def _run(loop: KernelLoop, session, chat):
    """跑一次会话运行：**显式传 spec**，与生产里 `run_chat_job` 的要求一致。"""
    return loop.run(session, run=chat.spec, store=chat.store, spec=EXECUTOR_SPEC)


def _setup(session, *, plan: bool):
    """一个独立项目 + 会话（可选已批准的确定性计划）+ 一行作业 + 一次会话运行。"""
    project = projects_dao.create(session, title="golden", goal=GOAL)
    conversation = conversations_dao.create(session, project_id=project.id, title="跑一遍")
    session.flush()
    if plan:
        steps = [
            planner.PlanStep(id=f"s{i}", title=f"第 {i} 步", status="pending").to_dict()
            for i in range(1, 3)
        ]
        task_plans_dao.create(
            session, conversation_id=conversation.id, steps=steps,
            mode=planner.PLAN_EXECUTE, deterministic=True, seed=0,
            title="golden 计划", status="approved",
        )
    job = jobs_dao.create(
        session, project_id=project.id, kind="chat",
        params={"conversation_id": conversation.id},
    )
    session.commit()
    chat = kernel_store.open_chat_run(
        session, project_id=project.id, conversation_id=conversation.id, job_id=job.id,
    )
    return project, conversation, job, chat


def _call_sequence(session, conversation_id: int) -> list[tuple]:
    """工具调用的可比形状：名字 + 参数 + 结果状态 + 权限档。"""
    return [
        (row.tool_name, json.dumps(row.args, ensure_ascii=False, sort_keys=True),
         row.status, row.permission)
        for row in tool_calls_dao.list_for_conversation(session, conversation_id)
    ]


def _event_types(session, job_id: int) -> list[str]:
    return [event.type for event in jobs_dao.events_after(session, job_id)]


def _artifact_digests(root: Path) -> dict[str, str]:
    """沙箱里每个文件的相对路径 → sha256。"""
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ── 一、计划器：完全不问模型，两次逐项相同 ────────────

def test_two_deterministic_plans_are_identical() -> None:
    """同一目标两次生成，步骤 / 顺序 / 标题 / seed 全部相同。"""
    template = planner.get_template(None)
    first = planner.build_plan(
        goal=GOAL, mode=planner.PLAN_EXECUTE, deterministic=True, template=template,
    )
    second = planner.build_plan(
        goal=GOAL, mode=planner.PLAN_EXECUTE, deterministic=True, template=template,
    )

    assert first.deterministic is True and second.deterministic is True
    assert first.mode == planner.PLAN_EXECUTE
    assert first.seed == second.seed
    assert first.title == second.title
    assert first.to_steps_payload() == second.to_steps_payload()
    assert [step.id for step in first.steps] == [step.id for step in second.steps]


# ── 二、温度：确定性下是 0.0，且非确定性下不是 ────────

def test_deterministic_runs_send_temperature_zero(session, tmp_path) -> None:
    _, _, _, chat = _setup(session, plan=True)
    gateway = ScriptedGateway(script())
    outcome = _run(_loop(gateway, tmp_path / "workspace"), session, chat)

    assert outcome.status == "succeeded", outcome.reason
    assert gateway.calls, "网关一次都没被调用"
    assert all(call["temperature"] == 0.0 for call in gateway.calls), [
        call["temperature"] for call in gateway.calls
    ]


def test_react_runs_do_not_send_temperature_zero(session, tmp_path) -> None:
    """反向的一半：没有确定性计划时用的是默认温度。

    少了这条，「deterministic → 0.0」可能只是某个常量恰好在两条路径上都成立。
    """
    _, _, _, chat = _setup(session, plan=False)
    gateway = ScriptedGateway(script())
    outcome = _run(_loop(gateway, tmp_path / "workspace"), session, chat)

    assert outcome.status == "succeeded", outcome.reason
    assert gateway.calls[0]["temperature"] == DEFAULT_TEMPERATURE
    assert gateway.calls[0]["temperature"] != 0.0


# ── 三、端到端：两个独立项目跑同一份脚本，三项逐项相等 ────

def test_the_same_task_twice_is_byte_identical(session, tmp_path) -> None:
    """golden case 本体。**两个独立项目**是关键：两次各有自己的沙箱目录，
    于是连「文件是否已存在」这种环境状态都一致，产物才可比。"""
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    runs = []
    for index, workspace in enumerate((workspace_a, workspace_b), start=1):
        project, conversation, job, chat = _setup(session, plan=True)
        gateway = ScriptedGateway(script())
        outcome = _run(_loop(gateway, workspace), session, chat)
        assert outcome.status == "succeeded", f"第 {index} 次没跑完：{outcome.reason}"
        runs.append({
            "calls": _call_sequence(session, conversation.id),
            "events": _event_types(session, job.id),
            "artifacts": _artifact_digests(workspace / f"project-{project.id}"),
            "assistant": [
                row.content for row in messages_dao.list_for_conversation(
                    session, conversation.id,
                ) if row.role == "assistant"
            ],
        })

    first, second = runs
    # ① 工具调用序列：名字 / 参数 / 状态 / 权限，逐项相等
    assert first["calls"] == second["calls"], (first["calls"], second["calls"])
    assert [row[0] for row in first["calls"]] == ["write_file", "read_file", "list_dir"]
    # ② 事件序列：类型顺序逐项相等（时长、id 这类合法差异不在其中）
    assert first["events"] == second["events"], (first["events"], second["events"])
    assert "tool.call" in first["events"] and "tool.result" in first["events"]
    # ③ 磁盘产物：文件名与内容（sha256）逐项相等
    assert first["artifacts"] == second["artifacts"], (first["artifacts"], second["artifacts"])
    assert first["artifacts"], "两次都没在沙箱里留下产物，那这条比对就是空的"
    # ④ 助手消息：模型说了什么也要一样
    assert first["assistant"] == second["assistant"]
