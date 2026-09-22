"""``AgentSpec`` 契约（US-404，对齐设计 §5.3）。

这份测试的价值在**对钉**：``AgentSpec`` 的同一批事实在代码里已经有三处来源 ——
``agents.demo_stage.STAGE_DEFS``（阶段与 Agent 名）、``agents_dao.STAGE_TIERS``（档位）、
``orchestration.orchestrator.STAGE_ORDER``（执行顺序）。加了第四处却不钉住，
漂移的表现会是「某个 Agent 的档位被悄悄降级」或「白名单漏了一个阶段」。

另一半价值是**负向断言**：§5.3 的权限最小化说的是「哪些工具**不在**白名单里」。
US-406 之前这条断言挡在「还没加工具」的前面；现在沙箱工具真的存在了，它挡的是
「漏给某个 Agent」——而那正是提示注入最想要的入口（处理文献内容的 Agent 拿到执行权限）。
"""

from __future__ import annotations

import pytest

from app.agent_kernel import specs
from app.agent_kernel.errors import KernelError
from app.agent_kernel.sandbox import SandboxPolicy
from app.agent_kernel.tools.factory import (
    SANDBOX_TOOL_NAMES,
    WITHHELD_TOOL_NAMES,
    build_registry,
)
from app.agents.demo_stage import STAGE_DEFS
from app.orchestration.orchestrator import STAGE_ORDER
from app.store.dao import agents as agents_dao

#: 唯一持有沙箱工具的 Agent（US-406）。§5.3 权限最小化落在这一条上：
#: 其余每个 Agent 的白名单都必须**不含** ``SANDBOX_TOOL_NAMES``。
SANDBOX_HOLDER = "executor"


# ── 与既有定义对钉 ──────────────────────────────

def test_stage_ids_and_agent_ids_match_stage_defs():
    declared = {(spec.stage, spec.id) for spec in specs.STAGE_AGENT_SPECS}
    expected = {(stage_id, agent_id) for stage_id, agent_id, *_ in STAGE_DEFS}
    assert declared == expected


def test_tier_matches_stage_tiers_table():
    """档位决定「用哪个模型」，两处各改一半会让某个阶段悄悄降级到便宜模型。"""
    for spec in specs.STAGE_AGENT_SPECS:
        assert spec.tier == agents_dao.STAGE_TIERS[spec.stage], spec.id


def test_spec_count_matches_stage_order():
    assert tuple(spec.stage for spec in specs.STAGE_AGENT_SPECS) == tuple(STAGE_ORDER)
    assert len(specs.STAGE_AGENT_SPECS) == len(STAGE_ORDER) == 8


def test_lookup_helpers_agree_with_the_tuple():
    for spec in specs.STAGE_AGENT_SPECS:
        assert specs.by_agent_id(spec.id) is spec
        assert specs.by_stage(spec.stage) is spec
    assert specs.by_agent_id("curator") is None
    assert specs.by_stage("S9") is None


# ── §5.3 字段取值 ──────────────────────────────

def test_human_checkpoint_follows_the_risk_grading():
    """§6.5：高风险（真实执行 / 写正式文件 / 对外发送）事前批，中风险事后审。

    含 S1 —— 「选定研究问题」是整条链上最贵的一次决策，设计把它归在「高」。
    """
    before = {"scout", "executor", "writer", "publisher"}
    after = {"librarian", "formalizer", "designer", "analyst"}
    by_id = {spec.id: spec for spec in specs.STAGE_AGENT_SPECS}
    assert {i for i, s in by_id.items() if s.human_checkpoint == specs.CHECKPOINT_BEFORE} == before
    assert {i for i, s in by_id.items() if s.human_checkpoint == specs.CHECKPOINT_AFTER} == after


def test_all_stages_require_critic():
    """设计 §5.2：Critic 独立校验**每个阶段**产物，有否决权。"""
    assert all(spec.requires_critic for spec in specs.STAGE_AGENT_SPECS)


def test_budgets_stay_at_the_existing_agents_table_defaults():
    """本步是纯结构改动，**不借机调预算**：顺手改会让运行差异无法归因。

    真要按阶段区分预算时，这里会一起改，那时它是一次有意为之的行为变更。
    """
    for spec in specs.STAGE_AGENT_SPECS:
        assert spec.max_steps == 20
        assert spec.max_cost_usd == 2.0


def test_reads_and_writes_are_declared_and_disjoint():
    """``scout`` 是入口：它没有上游产物可读，除此之外每个阶段都要声明读什么。

    读集合非空是**要挡的那件事**：不声明读什么，第 6 步的黑板权限就只能靠
    「不检查」，而「不检查」在权限系统里等于「全都能读」。
    """
    for spec in specs.STAGE_AGENT_SPECS:
        assert spec.writes, f"{spec.id} 没有声明任何可写对象类型"
        if spec.id != "scout":
            assert spec.reads, f"{spec.id} 没有声明任何可读对象类型"
        assert not set(spec.reads) & set(spec.writes), f"{spec.id} 读写集合重叠"
        assert len(set(spec.writes)) == len(spec.writes), f"{spec.id} 的 writes 有重复"
    assert specs.by_agent_id("scout").reads == ()


def test_blackboard_object_types_are_snake_case():
    for spec in specs.STAGE_AGENT_SPECS:
        for obj_type in spec.reads + spec.writes:
            assert obj_type == obj_type.lower() and " " not in obj_type, obj_type


def test_spec_is_frozen_and_uses_tuples():
    """冻结数据类里放 list，「不可变」就只停在字段级。"""
    spec = specs.STAGE_AGENT_SPECS[0]
    assert isinstance(spec.tools, tuple) and isinstance(spec.writes, tuple)
    with pytest.raises(Exception):  # noqa: B017
        spec.tools = ()  # type: ignore[misc]


def test_spec_rejects_illegal_checkpoint_and_budget():
    with pytest.raises(KernelError):
        specs.AgentSpec(id="x", stage="S9", tier="plan", human_checkpoint="maybe")
    with pytest.raises(KernelError):
        specs.AgentSpec(id="x", stage="S9", tier="plan", max_steps=0)
    with pytest.raises(KernelError):
        specs.AgentSpec(id="x", stage="S9", tier="plan", max_cost_usd=-1)
    with pytest.raises(KernelError):
        specs.AgentSpec(id="  ", stage="S9", tier="plan")


def test_checkpoint_constants_cover_design_literal():
    assert set(specs.HUMAN_CHECKPOINTS) == {"none", "before", "after", "risk_based"}


# ── 权限最小化：负向断言 ────────────────────────

def test_sandbox_tools_are_hold_only_by_executor():
    """US-406：沙箱工具**只**给 ``executor``，其余七个阶段与会话内核一律不含。

    这是本文件里最重要的一条。``SANDBOX_TOOL_NAMES`` 里既有只读的 ``read_file``
    也有 ``run_command`` —— 而一个「能读文件」的 Agent 与一个「能执行命令」的 Agent
    在威胁模型上是同一件事：``read_file`` 能读到的内容会被送进模型上下文，
    而上下文里的文本就是下一轮注入的载体。
    """
    for spec in specs.STAGE_AGENT_SPECS:
        leaked = set(spec.tools) & set(SANDBOX_TOOL_NAMES)
        if spec.id == SANDBOX_HOLDER:
            assert leaked == set(SANDBOX_TOOL_NAMES), (
                f"{spec.id} 应当持有全部沙箱工具，实际只持有 {sorted(leaked)}"
            )
        else:
            assert not leaked, f"{spec.id} 不该拿到沙箱工具 {sorted(leaked)}"


def test_conversation_kernel_holds_no_sandbox_tool():
    """会话内核的工具面必须最小：要跑命令得显式写 ``agent_id=executor``。

    「默认最严、提权要写明」是 §5.3 的落地方式。这里若漏了，
    「用户只是问了个问题」就能触发一次真实的命令执行。
    """
    assert not set(specs.CONVERSATION_SPEC.tools) & set(SANDBOX_TOOL_NAMES)


def test_literature_agents_hold_no_writing_or_network_tools():
    """§5.3 明文：「处理文献内容的 Agent 不持有文件写权限、不持有网络工具、不持有密钥」。

    这是 ``test_sandbox_tools_are_hold_only_by_executor`` 之外的**另一层**：
    它按「处理不可信输入」这个角度挑出那几个 Agent，把结论说成一句人能读的话。
    两层都留着不是冗余 —— 前者挡实现漂移，后者挡「有人按字母序把工具全给上」。
    """
    forbidden = {"write_file", "read_file", "list_dir", "glob", "grep", "run_command"}
    forbidden |= set(WITHHELD_TOOL_NAMES)
    for agent_id in ("librarian", "formalizer", "analyst", "writer"):
        tools = set(specs.by_agent_id(agent_id).tools)
        assert not tools & forbidden, f"{agent_id} 拿到了 {sorted(tools & forbidden)}"


def test_withheld_tools_are_absent_everywhere():
    """本版**不提供**的工具（``http_get``）不允许出现在任何白名单里。

    D6 的裁定是网络暴露面靠「没有工具」缩小，而不是靠「工具会自觉」。
    一个躺在白名单里、注册表里却没有的名字，会以「调用时被拒」的形式暴露给模型，
    读起来像一次偶发故障。
    """
    for spec in specs.ALL_AGENT_SPECS:
        assert not set(spec.tools) & set(WITHHELD_TOOL_NAMES), spec.id


def test_every_declared_tool_is_registered():
    """白名单里写了、注册表里没有 = 调用时必被拒。装配期只告警，所以这里替它把关。

    注册表用 ``build_registry`` 构造 —— 也就是生产用的那一个装配函数。
    测试自己拼一份工具清单的话，「测试绿、生产少一个工具」会一直藏着。
    """
    registry = build_registry(
        runner=lambda *a, **k: [], stage_ids=STAGE_ORDER,
        policy=SandboxPolicy.from_config("unused"),
    )
    assert specs.unknown_tools(set(registry.names())) == {}


def test_unknown_tools_reports_which_agents_declared_them():
    missing = specs.unknown_tools(set())
    assert missing["run_pipeline"] == [spec.id for spec in specs.ALL_AGENT_SPECS]
    # 沙箱工具只被 executor 声明，因此「没注册」时的责任人也只有它一个
    assert missing["run_command"] == [SANDBOX_HOLDER]
    assert missing["read_file"] == [SANDBOX_HOLDER]


def test_run_pipeline_is_allowed_for_every_stage_agent():
    """编排模板的每一步都要它（planner.RESEARCH_PIPELINE 的 tool 字段）。

    「每个阶段都能推进编排」看着像全放行，但真正的约束是负向的那一半：
    这八个白名单里**都不含**沙箱与网络工具。

    US-405 起会话内核（``kernel``）也在列表里 —— 会话要能推进编排（衔接约定 1），
    而它同样**不含**沙箱工具（US-406 之后仍然如此）。
    """
    assert specs.agents_allowing("run_pipeline") == [
        spec.id for spec in specs.ALL_AGENT_SPECS
    ]
    assert specs.agents_allowing("run_command") == [SANDBOX_HOLDER]
    assert specs.agents_allowing("read_file") == [SANDBOX_HOLDER]
    assert "kernel" in specs.agents_allowing("run_pipeline")


def test_support_agents_are_not_speculatively_declared():
    """Critic / Curator / Steward / Human（§5.2）还没有执行体，本步不建 spec。"""
    for agent_id in ("critic", "curator", "steward", "human"):
        assert specs.by_agent_id(agent_id) is None
