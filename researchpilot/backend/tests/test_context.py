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
from app.ai.base import ToolCall


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
    """D10：静默截断等于对模型撒谎 —— 必须留下截断标记。

    历史里**必须**同时有「发起调用的 assistant 消息」：US-405 起装配会做协议对齐，
    落单的 tool 结果会被丢掉（见 ``test_orphan_tool_result_is_dropped``）——
    这正是端点要的行为（结果找不到调用时，整条请求会被拒收）。
    """
    history = [
        ctx.ContextMessage("user", "跑一下"),
        ctx.ContextMessage(
            "assistant", "",
            tool_calls=(ToolCall(id="t1", name="echo", arguments="{}"),),
        ),
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


# ── 工具调用的协议对齐（US-405）──────────────────
#
# 端点的约束是**双向**的：``tool`` 结果必须能对应上带同名 ``tool_calls`` 的 assistant
# 消息，反之带 ``tool_calls`` 的 assistant 也必须跟齐结果。任一侧落单，**整条请求**
# 会被 400 拒收 —— 失败的不是那一条消息，是这一次对话。

def _call(call_id: str, name: str = "echo") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments="{}")


def test_orphan_tool_result_is_dropped():
    """结果找不到它的调用 → 丢掉，并记台账。留着会让整条请求发不出去。"""
    history = [
        ctx.ContextMessage("user", "问"),
        ctx.ContextMessage("tool", "孤儿结果", tool_call_id="ghost"),
    ]
    result = ctx.assemble(history, recent_turns=5)

    assert [m.role for m in result.messages] == ["user"]
    assert any("找不到对应调用" in note for note in result.notes)


def test_assistant_tool_calls_are_trimmed_to_answered_ones():
    """调用找不到结果 → 只保留仍有结果的那几条；全无结果则清空该字段。

    ``tool_calls=[]`` 同样非法，所以「全丢光」必须是 ``None`` 而不是空列表。
    """
    history = [
        ctx.ContextMessage("user", "问"),
        ctx.ContextMessage(
            "assistant", "",
            tool_calls=(_call("a1"), _call("a2"), _call("a3")),
        ),
        ctx.ContextMessage("tool", "只有第二个有结果", tool_call_id="a2"),
    ]
    result = ctx.assemble(history, recent_turns=5)

    assistant = next(m for m in result.messages if m.role == "assistant")
    assert [c.id for c in assistant.tool_calls] == ["a2"]
    assert any("没有结果回应" in note for note in result.notes)

    empty = ctx.assemble(
        [
            ctx.ContextMessage("user", "问"),
            ctx.ContextMessage("assistant", "没有工具的一轮", tool_calls=(_call("b1"),)),
        ],
        recent_turns=5,
    )
    kept_assistant = next(m for m in empty.messages if m.role == "assistant")
    assert kept_assistant.tool_calls is None


def test_matched_pairs_pass_through_untouched():
    """配对完整时**一点都不动** —— 兜底逻辑只该在真的坏掉时才生效。"""
    history = [
        ctx.ContextMessage("user", "跑一下"),
        ctx.ContextMessage("assistant", "", tool_calls=(_call("c1"),)),
        ctx.ContextMessage("tool", "结果", tool_call_id="c1"),
    ]
    result = ctx.assemble(history, recent_turns=5)

    assert [m.role for m in result.messages] == ["user", "assistant", "tool"]
    assistant = result.messages[1]
    assert [c.id for c in assistant.tool_calls] == ["c1"]
    assert not any("丢弃了" in note or "清掉了" in note for note in result.notes)


def test_tool_call_arguments_count_toward_tokens():
    """工具调用的参数在回放时确实要占上游窗口，估算必须算上它。

    不算的话裁剪会以为这条消息比实际小 —— ``tokens`` 模块的约定是宁可偏高。
    """
    without = ctx.ContextMessage("assistant", "")
    with_calls = ctx.ContextMessage(
        "assistant", "",
        tool_calls=(ToolCall(id="d1", name="echo", arguments='{"text": "很长的一段参数"}'),),
    )
    assert with_calls.tokens > without.tokens


def test_context_message_reads_tool_calls_from_row_duck_typed():
    """读路径**不抛错**：一条形状不对的历史记录不该让整个会话装配失败。"""
    class Row:
        role = "assistant"
        content = ""
        tool_call_id = None
        tool_calls = [
            {"id": "e1", "name": "echo", "arguments": "{}"},
            "这条不是对象，应当被跳过",
            {"name": ""},  # 缺 name 也跳过
        ]

    message = ctx.ContextMessage.from_row(Row())
    assert [c.id for c in message.tool_calls] == ["e1"]
