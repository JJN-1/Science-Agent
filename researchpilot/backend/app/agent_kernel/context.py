"""上下文装配与裁剪（US-402 / D8）。

裁剪优先级**固定不可配置**（系统提示 > 当前计划 > 最近 N 轮 > 工具结果摘要 >
更早轮次摘要化）。做成可配置就意味着某个调用点可以把自己那部分调到最前面，
「谁的上下文更长谁先说话」——这一类 bug 只在长会话里出现，很难回头查。

本模块是纯函数：不碰数据库、不调模型。``history`` 由调用方从 ``messages`` 表读出来，
``summarizer`` 由调用方注入。**注入模型摘要器时，那次调用的花费必须由调用方计入预算**
（D8）—— 否则「省 token」的手段本身会变成最贵的一步。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.agent_kernel.errors import ContextBudgetError
from app.agent_kernel.tokens import (
    MESSAGE_OVERHEAD_TOKENS,
    estimate_message_tokens,
    estimate_tokens,
)

# ── 槽位（常数顺序 = 裁剪梯队顺序，见 D8）──────
SLOT_SYSTEM = "system"
SLOT_PLAN = "plan"
SLOT_RECENT = "recent"
SLOT_TOOL = "tool"
SLOT_HISTORY = "history"

#: 默认上下文预算（token）。可按档位覆盖，见 Sprint 4 计划的 planner。
DEFAULT_BUDGET_TOKENS = 8192
#: 默认保留最近多少轮完整对话
DEFAULT_RECENT_TURNS = 8
#: 本地摘要里每一轮各保留多少字符（问 / 答各一份）
DIGEST_CHARS_PER_TURN = 160
#: 压缩文本时给「已截断」标记预留的 token
MARK_RESERVE_TOKENS = 16
#: 单个工具结果在上下文里的字符上限（D10 的上下文侧对应物）
TOOL_RESULT_MAX_CHARS = 2000

_TRUNCATE_MARK = "…[已截断 {n} 字符]…"


@dataclass(frozen=True)
class ContextMessage:
    """装配单元：与 ORM 解耦，内核层不反向依赖 store。"""

    role: str
    content: str
    tool_call_id: str | None = None

    @property
    def tokens(self) -> int:
        return estimate_message_tokens(self.role, self.content)

    @classmethod
    def from_row(cls, row: Any) -> ContextMessage:
        """从 ``messages`` 表的一行构造（鸭子类型，刻意不 import ORM 模型）。"""
        return cls(
            role=getattr(row, "role", "user"),
            content=getattr(row, "content", "") or "",
            tool_call_id=getattr(row, "tool_call_id", None),
        )


@dataclass(frozen=True)
class AssemblyResult:
    """装配结果。附带完整的裁剪台账 —— 「为什么这轮对话少了东西」必须答得出来。"""

    messages: list[ContextMessage]
    budget_tokens: int
    used_tokens: int
    kept_turns: int
    collapsed_turns: int
    dropped_turns: int
    summarized: bool
    notes: tuple[str, ...] = ()

    @property
    def headroom_tokens(self) -> int:
        return max(self.budget_tokens - self.used_tokens, 0)


Summarizer = Callable[[Sequence[Sequence[ContextMessage]]], str]


# ── 内部结构 ────────────────────────────────────

@dataclass
class _Item:
    slot: str
    message: ContextMessage


@dataclass
class _Turn:
    items: list[_Item]

    @property
    def tokens(self) -> int:
        return sum(item.message.tokens for item in self.items)


def group_turns(messages: Sequence[ContextMessage]) -> list[list[ContextMessage]]:
    """把消息切成「轮」：一条 user 消息 + 其后直到下一条 user 之前的全部消息。

    首条 user 之前若有孤立消息（如系统注入的工具结果），**自成第 0 轮**而不是丢弃 ——
    丢掉会让「工具返回了什么」在后续推理里凭空消失；自成一轮则还能被单独裁掉，
    既保留了事实，也留了后路。
    """
    turns: list[list[ContextMessage]] = []
    current: list[ContextMessage] = []
    for message in messages:
        if message.role == "user" and current:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)
    return turns


# ── 文本压缩 ────────────────────────────────────

def _truncate_to_tokens(role: str, text: str, target_tokens: int) -> str:
    """按比例收缩直到装进 ``target_tokens``，并留下可见的截断标记。

    标记本身也要占 token，所以先给它留出额度（``MARK_RESERVE_TOKENS``）——
    否则「刚好装下」的文本加上标记就溢出了，等于把溢出从一步推给了下一步。
    """
    if estimate_message_tokens(role, text) <= target_tokens:
        return text
    content_target = max(
        target_tokens - estimate_message_tokens(role, "") - MARK_RESERVE_TOKENS, 1,
    )
    kept = len(text)
    for _ in range(12):
        kept = int(kept * 0.7)
        if kept <= 1:
            break
        candidate = text[:kept]
        if estimate_tokens(candidate) <= content_target:
            mark = _TRUNCATE_MARK.format(n=len(text) - kept)
            return candidate + mark
    mark = _TRUNCATE_MARK.format(n=max(len(text) - 1, 0))
    return text[:1] + mark


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _digest(turns: Sequence[Sequence[ContextMessage]]) -> str:
    """本地摘要：每轮只留第一句问与最后一句答的前若干字符。

    ⚠️ **这是有损的，而且损失恰好是最关键的部分**：它保留「问了什么/答了什么」，
    但丢掉「为什么否决了另一个方案」。设计 §5.2 的 Curator 正是为补这个洞而立项，
    它不在 Sprint 4 范围内。在此之前，长会话里被压缩掉的决策理由就是真的没了。
    """
    lines = [f"[更早 {len(turns)} 轮对话摘要 · 本地生成，已丢弃细节]"]
    for index, turn in enumerate(turns, start=1):
        question = next((m for m in turn if m.role == "user"), None)
        answer = next(
            (m for m in reversed(turn) if m.role in ("assistant", "tool")), None,
        )
        parts: list[str] = []
        if question is not None:
            parts.append("问：" + _clip(question.content, DIGEST_CHARS_PER_TURN))
        if answer is not None:
            label = "答：" if answer.role == "assistant" else "工具："
            parts.append(label + _clip(answer.content, DIGEST_CHARS_PER_TURN))
        lines.append(f"{index}. " + (" | ".join(parts) if parts else "（空轮）"))
    return "\n".join(lines)


# ── 装配 ────────────────────────────────────────

def assemble(
    history: Sequence[ContextMessage],
    *,
    system: str = "",
    plan: str = "",
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    recent_turns: int = DEFAULT_RECENT_TURNS,
    summarizer: Summarizer | None = None,
) -> AssemblyResult:
    """装配一次调用的消息列表。

    裁剪梯队（只在超出预算时才逐级下探，每级都记台账）：

    1. 更早轮次 → 摘要（无论是否超窗都会做，这是正常路径而非降级）
    2. 摘要过长 → 就地压缩
    3. 工具结果过长 → 头尾保留 + 截断标记
    4. 仍超 → 由旧到新整轮丢弃
    5. 仍超 → 丢掉当前计划
    6. 仍超 → 抛 ``AGENT-CTX-001``（此时只剩系统提示，已无可丢）
    """
    notes: list[str] = []
    pinned: list[_Item] = []
    if system.strip():
        pinned.append(_Item(SLOT_SYSTEM, ContextMessage("system", system)))
    plan_item: _Item | None = None
    if plan.strip():
        plan_item = _Item(SLOT_PLAN, ContextMessage("system", plan))
        pinned.append(plan_item)

    turns = group_turns(history)
    keep = max(recent_turns, 0)
    older = turns[:-keep] if keep else list(turns)
    recent = turns[-keep:] if keep else []

    collapsed_turns = len(older)
    history_items: list[_Item] = []
    if older:
        text = summarizer(older) if summarizer is not None else _digest(older)
        history_items.append(_Item(SLOT_HISTORY, ContextMessage("system", text)))
        notes.append(f"更早 {collapsed_turns} 轮已摘要（保留最近 {len(recent)} 轮完整）")

    kept: list[_Turn] = []
    for turn in recent:
        items = [
            _Item(SLOT_TOOL if m.role == "tool" else SLOT_RECENT, m) for m in turn
        ]
        kept.append(_Turn(items))

    def total() -> int:
        return (
            sum(item.message.tokens for item in pinned)
            + sum(item.message.tokens for item in history_items)
            + sum(turn.tokens for turn in kept)
        )

    system_tokens = sum(
        item.message.tokens for item in pinned if item.slot == SLOT_SYSTEM
    )
    if system_tokens > budget_tokens:
        raise ContextBudgetError(
            f"系统提示自身 {system_tokens} tokens 已超过预算 {budget_tokens}，"
            "没有可裁剪的对象；请调大预算或缩短系统提示"
        )

    # 第 2 级：摘要就地压缩
    if total() > budget_tokens and history_items:
        room = budget_tokens - (
            sum(item.message.tokens for item in pinned)
            + sum(turn.tokens for turn in kept)
        )
        if room <= MESSAGE_OVERHEAD_TOKENS:
            notes.append("摘要已丢弃（最近轮次与系统提示已占满预算）")
            history_items = []
        else:
            item = history_items[0]
            shrunk = _truncate_to_tokens(item.message.role, item.message.content, room)
            if shrunk != item.message.content:
                item.message = ContextMessage(
                    item.message.role, shrunk, item.message.tool_call_id,
                )
                notes.append("摘要被进一步压缩")

    # 第 3 级：工具结果头尾保留（D10：静默截断等于对模型撒谎）
    if total() > budget_tokens:
        shrunk_any = False
        for turn in kept:
            for item in turn.items:
                if item.slot != SLOT_TOOL:
                    continue
                content = item.message.content
                if len(content) > TOOL_RESULT_MAX_CHARS:
                    head = TOOL_RESULT_MAX_CHARS * 2 // 3
                    tail = TOOL_RESULT_MAX_CHARS - head
                    item.message = ContextMessage(
                        item.message.role,
                        content[:head]
                        + _TRUNCATE_MARK.format(n=len(content) - TOOL_RESULT_MAX_CHARS)
                        + content[-tail:],
                        item.message.tool_call_id,
                    )
                    shrunk_any = True
        if shrunk_any:
            notes.append(f"工具结果已按 {TOOL_RESULT_MAX_CHARS} 字符头尾保留")

    # 第 4 级：由旧到新整轮丢弃
    dropped_turns = 0
    while total() > budget_tokens and kept:
        kept.pop(0)
        dropped_turns += 1
    if dropped_turns:
        notes.append(f"因预算不足丢弃了 {dropped_turns} 轮最近的对话（由旧到新）")

    # 第 5 级：丢掉当前计划（计划排在系统提示之后，是最后一个可丢的东西）
    if total() > budget_tokens and plan_item is not None:
        pinned = [item for item in pinned if item is not plan_item]
        notes.append("当前计划已丢弃（预算仅够系统提示与最近对话）")

    used = total()
    if used > budget_tokens:
        # 到这里只剩系统提示与可能的一条历史摘要，理论上不该发生
        raise ContextBudgetError(
            f"裁剪后仍占 {used} tokens（预算 {budget_tokens}）"
        )

    messages = [item.message for item in pinned]
    messages += [item.message for item in history_items]
    messages += [item.message for turn in kept for item in turn.items]

    return AssemblyResult(
        messages=messages,
        budget_tokens=budget_tokens,
        used_tokens=used,
        kept_turns=len(kept),
        collapsed_turns=collapsed_turns,
        dropped_turns=dropped_turns,
        summarized=collapsed_turns > 0,
        notes=tuple(notes),
    )
