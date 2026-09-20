"""上下文装配与裁剪（US-402）。

这里守的不是「能装进去」，而是**裁剪的次序与台账**。裁剪是静默的：装错了没人会报错，
只会表现为「模型忘了刚才说过的话」。所以每个降级台阶都要有一条测试钉住它，
并且要断言 notes 里记了这件事 —— 没记就等于没发生。
"""

from __future__ import annotations

import pytest

from app.agent_kernel import context as ctx
from app.agent_kernel.errors import ContextBudgetError
from app.agent_kernel.tokens import estimate_message_tokens, estimate_tokens


def _turn(index: int, *, answer: str = "答") -> list[ctx.ContextMessage]:
    return [
        ctx.ContextMessage("user", f"第{index}轮的问题"),
        ctx.ContextMessage("assistant", f"{answer}{index}"),
    ]


def _history(turns: int) -> list[ctx.ContextMessage]:
    messages: list[ctx.ContextMessage] = []
    for index in range(turns):
        messages.extend(_turn(index))
    return messages


# ── 估算 ────────────────────────────────────────

def test_estimate_tokens_counts_cjk_and_ascii_differently():
    """CJK 按 1 字符 1 token，ASCII 按 3 字符 1 token —— 两种粒度不能混成一个比值。"""
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcdef") == 2
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_is_bounded_and_whitespace_insensitive():
    """缩进与换行不该把估算吹起来；估算必须是「宁可偏高」而不是失控。"""
    assert estimate_tokens("a\n\n\n    b") == estimate_tokens("a b")
    # 保守方向：估算不低于真实值的一半（同长度中文 vs 代码的极端对比）
    assert estimate_tokens("字" * 100) == 100
    assert estimate_tokens("a" * 100) == 34


def test_estimate_message_tokens_includes_overhead():
    assert estimate_message_tokens("user", "hi") > estimate_tokens("hi")


# ── 分轮 ────────────────────────────────────────

def test_group_turns_splits_on_user_messages():
    turns = ctx.group_turns(_history(3))
    assert len(turns) == 3
    assert [m.role for m in turns[1]] == ["user", "assistant"]


def test_group_turns_keeps_leading_orphan_messages():
    """首条 user 之前的孤立消息自成第 0 轮，而不是被丢掉。

    丢掉会让「工具返回了什么」在后续推理里凭空消失；自成一轮则还能被单独裁掉 ——
    既能保留，也留了后路。
    """
    history = [
        ctx.ContextMessage("tool", "工具结果", tool_call_id="t1"),
        *_turn(0),
    ]
    turns = ctx.group_turns(history)
    assert len(turns) == 2
    assert [m.role for m in turns[0]] == ["tool"]
    assert [m.role for m in turns[1]] == ["user", "assistant"]


# ── 正常路径：更早轮次摘要化 ──────────────────────

def test_recent_turns_kept_intact_and_older_collapsed():
    long_tail = "细节" * 200
    history: list[ctx.ContextMessage] = []
    for index in range(12):
        filler = long_tail if index < 8 else ""
        history.append(ctx.ContextMessage("user", f"第{index}轮的问题{filler}"))
        history.append(ctx.ContextMessage("assistant", f"答{index}{filler}"))

    result = ctx.assemble(history, system="sys", budget_tokens=4000, recent_turns=4)

    assert result.kept_turns == 4
    assert result.collapsed_turns == 8
    assert result.summarized is True

    digests = [m for m in result.messages if "更早 8 轮对话摘要" in m.content]
    assert len(digests) == 1, "被压的 8 轮必须收进一条摘要，而不是散成 8 条"
    # 摘要确实压过：长内容留下省略号，而不是原样搬运
    assert "…" in digests[0].content

    # 最近 4 轮逐字保留，且是独立消息（没有被并进摘要里）
    assert [m.content for m in result.messages if m.role == "user"] == [
        f"第{i}轮的问题" for i in range(8, 12)
    ]


def test_no_history_means_no_digest():
    result = ctx.assemble([], system="sys", plan="plan")
    assert result.summarized is False
    assert result.collapsed_turns == 0
    assert [m.role for m in result.messages] == ["system", "system"]


def test_injected_summarizer_replaces_local_digest():
    """D8：摘要器的模型调用由调用方计预算，所以这里只验接口被真正使用。"""
    seen: list[int] = []

    def summarizer(turns):
        seen.append(len(turns))
        return "外部摘要器产物"

    result = ctx.assemble(_history(6), summarizer=summarizer, recent_turns=2)
    assert seen == [4]
    assert any("外部摘要器产物" in m.content for m in result.messages)


# ── 裁剪梯队 ────────────────────────────────────

def test_tool_results_are_truncated_with_visible_marker():
    """D10：静默截断等于对模型撒谎 —— 必须留下截断标记。"""
    history = [
        ctx.ContextMessage("user", "跑一下"),
        ctx.ContextMessage("tool", "x" * 8000, tool_call_id="t1"),
    ]
    result = ctx.assemble(history, budget_tokens=1500, recent_turns=5)

    tool_messages = [m for m in result.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    content = tool_messages[0].content
    assert "已截断" in content
    assert len(content) < 8000
    assert any("工具结果已按" in note for note in result.notes)


def test_oldest_turns_dropped_before_plan():
    """计划的存活优先级高于最近的轮次（D8），所以先丢轮次。"""
    history = _history(10)
    result = ctx.assemble(history, system="s", plan="当前计划：先做 A 再做 B",
                          budget_tokens=200, recent_turns=8)

    assert result.dropped_turns > 0
    assert any("丢弃了" in note for note in result.notes)
    assert not any("当前计划已丢弃" in note for note in result.notes)
    assert any("先做 A" in m.content for m in result.messages)


def test_plan_dropped_when_nothing_else_left():
    """预算被系统提示吃掉后，最后可丢的就是计划本身。

    预算定在「系统提示装得下、系统提示 + 计划装不下」，才能把梯队走到最后一级 ——
    预算稍微宽松一点，丢一两轮对话就够用了，计划根本轮不到被丢。
    """
    result = ctx.assemble(
        _history(6),
        system="s" * 60,
        plan="当前计划：先做 A 再做 B",
        budget_tokens=32,
        recent_turns=8,
    )
    assert any("当前计划已丢弃" in note for note in result.notes)
    assert any("丢弃了" in note for note in result.notes)
    assert not any("先做 A" in m.content for m in result.messages)
    assert result.used_tokens <= result.budget_tokens


def test_system_prompt_over_budget_raises_with_code():
    """唯一该硬失败的情形：连系统提示都装不下，再往下没有可丢的对象。"""
    with pytest.raises(ContextBudgetError) as excinfo:
        ctx.assemble(_history(3), system="字" * 500, budget_tokens=100)
    assert "AGENT-CTX-001" in str(excinfo.value)


def test_used_tokens_never_exceeds_budget():
    """任何输入都必须收敛到预算内 —— 溢出会让上游直接 400。"""
    for budget in (150, 300, 600, 1200, 3000):
        result = ctx.assemble(
            _history(20), system="sys" * 10, plan="计划" * 20,
            budget_tokens=budget, recent_turns=6,
        )
        assert result.used_tokens <= budget, f"budget={budget} 时溢出"
        assert result.headroom_tokens >= 0


def test_notes_always_state_what_was_altered():
    """裁剪台账：发生了降级就必须有人能回答「少了什么」。"""
    result = ctx.assemble(_history(11), budget_tokens=900, recent_turns=4)
    assert result.notes
    assert any("摘要" in note for note in result.notes)
