"""计划器（US-403）。

这里最要紧的两条：

1. **确定性计划不许问模型**。它不是「顺手保持一致」，而是确定性模式能成立的唯一前提
   —— 问一次就多一个不可复现的输入，而 G2 第 7 条要拿「两次跑出相同步骤序列」当证据。
2. **模板与既有的阶段定义不许漂移**。模板把 8 个阶段抄了一份（内核层不反向依赖
   orchestration / agents），所以必须有一条测试盯着这两份清单是否还一致。
"""

from __future__ import annotations

import pytest

from app.agent_kernel import planner
from app.agent_kernel.errors import PlanError


def _payload(steps=None) -> dict:
    return {
        "title": "模型计划",
        "rationale": "模型给的理由",
        "steps": steps
        if steps is not None
        else [
            {"id": "a", "title": "第一步", "intent": "探路", "tool": "read_file",
             "params": {"path": "x.txt"}},
            {"id": "b", "title": "第二步"},
        ],
    }


# ── 与既有定义的一致性 ──────────────────────────

def test_template_matches_stage_order_and_definitions():
    """模板自带一份阶段清单，必须与编排层/阶段定义逐项一致。

    内核层刻意不 import 那两个模块（分层方向），代价就是这份重复；
    用测试而不是靠自觉来保证它不漂移。
    """
    from app.agents.demo_stage import STAGE_DEFS
    from app.orchestration.orchestrator import STAGE_ORDER

    assert [s.id for s in planner.RESEARCH_PIPELINE.steps] == STAGE_ORDER

    titles = {stage_id: name for stage_id, _agent, name, *_rest in STAGE_DEFS}
    for step in planner.RESEARCH_PIPELINE.steps:
        assert step.title == titles[step.id], f"{step.id} 的名称与 STAGE_DEFS 不一致"


def test_template_steps_all_target_run_pipeline():
    """两阶段衔接约定 1：编排要在内核里可达 —— 模板先把这件事写成可断言的形式。"""
    for step in planner.RESEARCH_PIPELINE.steps:
        assert step.tool == "run_pipeline"
        assert step.params == {"stage_ids": [step.id]}


def test_vocabulary_matches_store_vocabulary():
    """内核层与存储层各有一份词表（同样为了分层），用测试钉住两者相等。"""
    from app.store.models import PLAN_MODES, PLAN_STEP_STATUSES

    assert planner.VALID_MODES == PLAN_MODES
    assert planner.VALID_STEP_STATUSES == PLAN_STEP_STATUSES


# ── 确定性模式 ──────────────────────────────────

def test_deterministic_plan_never_asks_the_model():
    """**确定性模式完全不触碰模型**：它不是「顺便一致」，而是可复现的前提。"""
    def forbidden(goal: str) -> dict:
        raise AssertionError("确定性模式不该调用模型提案")

    plan = planner.build_plan(goal="图神经网络", deterministic=True, propose=forbidden)

    assert plan.deterministic is True
    assert plan.mode == "plan_execute"
    assert plan.seed == planner.DETERMINISTIC_SEED
    assert "未经模型改写" in plan.rationale
    assert plan.step_ids == tuple(s.id for s in planner.RESEARCH_PIPELINE.steps)


def test_two_deterministic_builds_produce_identical_step_sequences():
    """G2 第 7 条的判据：同一输入两次，步骤序列**逐项相等**。"""
    first = planner.build_plan(goal="同一目标", deterministic=True)
    second = planner.build_plan(goal="同一目标", deterministic=True)

    assert first.steps == second.steps
    assert first.to_steps_payload() == second.to_steps_payload()
    assert first.seed == second.seed


def test_deterministic_rejects_react_mode():
    """D7：deterministic 恒为 plan_execute —— ReAct 的下一步由观察决定，与固定顺序互斥。"""
    with pytest.raises(PlanError) as excinfo:
        planner.build_plan(mode="react", deterministic=True)
    assert excinfo.value.code == "AGENT-PLAN-002"
    assert "plan_execute" in str(excinfo.value)


def test_unknown_mode_rejected():
    with pytest.raises(PlanError) as excinfo:
        planner.build_plan(mode="group_chat")
    assert excinfo.value.code == "AGENT-PLAN-001"


def test_template_cannot_serve_react_mode():
    """模板是一份**预先写死**的步骤序列，react 的下一步由观察决定 —— 两者互斥。

    这里必须是**报错**而不是回退：若降级成 plan_execute，用户拿到的计划会写着
    ``mode=react`` 而内容是固定八步，界面标注与实际行为不一致（G2 第 8 条）。
    """
    with pytest.raises(PlanError) as excinfo:
        planner.build_plan(mode="react")
    assert excinfo.value.code == "AGENT-PLAN-002"
    assert "react" in str(excinfo.value)


def test_failed_proposal_does_not_silently_downgrade_react_to_plan_execute():
    """提案失败也走模板回退，所以这条路径同样不能悄悄换掉 mode。"""
    def boom(goal: str) -> dict:
        raise RuntimeError("上游 502")

    with pytest.raises(PlanError) as excinfo:
        planner.build_plan(goal="目标", mode="react", propose=boom)
    assert excinfo.value.code == "AGENT-PLAN-002"


def test_successful_proposal_keeps_the_requested_mode():
    """提案成功时 mode 由请求决定 —— 排斥只发生在「模板」这一条来源上。"""
    proposal = {"steps": [{"id": "a", "title": "先做一步"}]}
    plan = planner.build_plan(goal="目标", mode="react", propose=lambda _g: proposal)
    assert plan.mode == "react"
    assert plan.step_ids == ("a",)


# ── 模型提案 ────────────────────────────────────

def test_proposal_is_used_when_available():
    plan = planner.build_plan(goal="目标", propose=lambda _goal: _payload())

    assert plan.step_ids == ("a", "b")
    assert plan.title == "模型计划"
    assert plan.deterministic is False
    assert plan.seed is None


def test_invalid_proposal_falls_back_to_template_and_says_so():
    """回退必须留痕：静默换成模板，用户会以为这是模型想出来的计划。"""
    plan = planner.build_plan(goal="目标", propose=lambda _goal: {"nope": 1})

    assert plan.step_ids == tuple(s.id for s in planner.RESEARCH_PIPELINE.steps)
    assert "模型提案不可用" in plan.rationale
    assert "steps" in plan.rationale  # 带上违规说明，便于定位


def test_proposal_exception_falls_back_instead_of_breaking_planning():
    def boom(_goal: str) -> dict:
        raise RuntimeError("上游 502")

    plan = planner.build_plan(goal="目标", propose=boom)

    assert plan.step_ids == tuple(s.id for s in planner.RESEARCH_PIPELINE.steps)
    assert "上游 502" in plan.rationale


def test_schema_violation_reports_the_offending_path():
    bad = {"steps": [{"id": "a"}]}  # 缺 title
    with pytest.raises(PlanError) as excinfo:
        planner.plan_from_payload(bad)
    assert "$.steps[0]" in str(excinfo.value)


# ── 步骤校验 ────────────────────────────────────

def test_duplicate_step_ids_rejected():
    """检查点靠 step id 定位；重复 id 会让「从第 3 步继续」没有唯一解。

    两步都必须**合法**（带 title）—— 否则会先撞上缺字段的校验，
    这个测试就再也证明不了「重复 id 会被拒」。
    """
    with pytest.raises(PlanError) as excinfo:
        planner.normalize_steps(
            [{"id": "s", "title": "第一步"}, {"id": "s", "title": "第二步"}]
        )
    assert "重复" in str(excinfo.value)


def test_empty_step_list_rejected():
    with pytest.raises(PlanError):
        planner.normalize_steps([])


@pytest.mark.parametrize(
    ("raw", "hint"),
    [
        ({"title": "无 id"}, "缺少 id"),
        ({"id": "x"}, "缺少 title"),
        ({"id": "x", "title": "t", "status": "bogus"}, "状态非法"),
        ({"id": "x", "title": "t", "params": "not-an-object"}, "params"),
        ("不是对象", "必须是对象"),
    ],
)
def test_malformed_steps_rejected(raw, hint):
    with pytest.raises(PlanError) as excinfo:
        planner.normalize_steps([raw])
    assert hint in str(excinfo.value)


def test_blank_tool_becomes_none():
    steps = planner.normalize_steps([{"id": "x", "title": "t", "tool": "   "}])
    assert steps[0].tool is None


# ── 编辑规则 ────────────────────────────────────

def test_ensure_editable_only_allows_draft():
    planner.ensure_editable("draft")
    for status in ("approved", "executing", "done", "failed"):
        with pytest.raises(PlanError) as excinfo:
            planner.ensure_editable(status)
        assert excinfo.value.code == "AGENT-PLAN-003"


def test_ensure_approvable_only_allows_draft():
    planner.ensure_approvable("draft")
    with pytest.raises(PlanError) as excinfo:
        planner.ensure_approvable("approved")
    assert excinfo.value.code == "AGENT-PLAN-003"


def test_deterministic_plan_refuses_step_edits_but_allows_title():
    """执行只读 steps，所以改标题不影响可复现性；改步骤就毁了确定性。"""
    plan = planner.build_plan(deterministic=True)

    renamed = planner.revise_plan(plan, title="换个名字")
    assert renamed.title == "换个名字"
    assert renamed.steps == plan.steps

    with pytest.raises(PlanError) as excinfo:
        planner.revise_plan(plan, steps=[{"id": "x", "title": "新步骤"}])
    assert excinfo.value.code == "AGENT-PLAN-003"


def test_non_deterministic_plan_can_replace_steps():
    plan = planner.build_plan()
    revised = planner.revise_plan(plan, steps=[{"id": "only", "title": "唯一一步"}])
    assert revised.step_ids == ("only",)


# ── 行读取 ──────────────────────────────────────

class _Row:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


def test_plan_from_row_roundtrips():
    plan = planner.build_plan(deterministic=True)
    row = _Row(
        steps=plan.to_steps_payload(), mode=plan.mode, deterministic=plan.deterministic,
        seed=plan.seed, title=plan.title, rationale=plan.rationale,
    )
    assert planner.Plan.from_row(row) == plan


def test_counts_tally_step_statuses():
    plan = planner.normalize_steps([
        {"id": "a", "title": "A", "status": "done"},
        {"id": "b", "title": "B", "status": "done"},
        {"id": "c", "title": "C"},
    ])
    counts = planner.Plan(steps=plan).counts()
    assert counts["done"] == 2
    assert counts["pending"] == 1
    assert counts["failed"] == 0


def test_unknown_template_rejected():
    with pytest.raises(PlanError) as excinfo:
        planner.get_template("no-such-template")
    assert "research_pipeline" in str(excinfo.value)
