"""内核检查点与续跑点判定（US-407）。

设计 §778 把这张表一句话说完了：**「内核检查点（计划、步骤序号、快照），支持中断后恢复」**。
本模块负责后半句里所有**判断**，落库形状（``Checkpoint``）也放在这里，因为两者共享
同一份「快照里有什么」的约定。

## 一、判定全部是纯函数

「这条检查点能不能续跑、该从哪一步接着跑」是这一步里**最容易写错、又最难在真机上试出来**
的一段：写错不会让任何东西崩，只会让恢复后的循环从错误的位置继续 ——
表现成「它把已经做过的第 2 步又做了一遍」或者「第 4 步被整个跳过」。
而这两种表现从界面上看都像「正常跑完了」，用户无从察觉。

所以判定一律放这里、一律纯函数、不碰数据库：每一条分支都能用十几行单测钉死。
落库那一层（``app/store/dao/kernel_checkpoints.py``）只负责搬运。

## 二、定位靠 ``PlanStep.id``，不靠序号

``PlanStep.id`` 是稳定标识而不是下标（US-403 的铁律）。用下标恢复，只要计划中间插了
一步，游标就整体错位 —— 而且错位之后循环**不会报错**，它会一本正经地把错的那一步
当成对的那一步跑完。``step_index`` 只用于人读和「序号单调不减」这类断言。

## 三、谁决定「能不能」、谁决定「从哪」

- **计划**（``task_plans.steps[*].status``）是**活的事实**，它决定「从哪接着跑」：
  第一个状态既不是 ``done`` 也不是 ``skipped`` 的步骤就是续跑点。
- **检查点**决定「能不能续」、并携带「上次为什么停」：``done`` 之后再点「继续」是
  **重跑**（用户可能就是想再来一次）；``paused`` / ``cancelled`` / ``running`` 之后是**续跑**。

两者会不一致（检查点写于上一次运行的末尾，而计划随后又被推进过）。此时**以计划为准**，
但把分歧记进 ``ResumeDecision.stale`` —— 不静默、也不因此拒绝续跑：
拒绝会让用户面对一个「内部记账不一致」的死局，而它其实完全可以继续跑。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.agent_kernel.planner import Plan

# ── 检查点状态 ──────────────────────────────────
#: 正在跑。**上一进程死在半路时留下的就是它**：进程没有机会写终态，
#: 所以它必须算「可续跑」——否则一次崩溃就等于这个会话永久报废。
RUNNING = "running"
#: 正常挂起（等批准 / 预算熔断）。它本来就等着人把它放回去。
PAUSED = "paused"
#: 人主动中止。与 ``failed`` 的区别是：中止是**可续**的，失败不是。
CANCELLED = "cancelled"
#: 正常跑完。这之后再点「继续」是重跑。
DONE = "done"

VALID_STATUSES: tuple[str, ...] = (RUNNING, PAUSED, CANCELLED, DONE)

#: 可以续跑的状态。``done`` 刻意不在其中。
RESUMABLE_STATUSES: tuple[str, ...] = (RUNNING, PAUSED, CANCELLED)

#: 计划里「这一步已经过去」的状态。``failed`` **不在此列**：那一步没做成，
#: 续跑时该重新面对它，而不是当成已经过去。
SETTLED_STATUSES: tuple[str, ...] = ("done", "skipped")

#: 快照里**必须**有的键。少一个就会让某条恢复路径只能靠猜 ——
#: 例如少了 ``rounds``，「这个会话一共花了多少轮」在恢复之后就再也答不上来。
SNAPSHOT_KEYS: tuple[str, ...] = (
    "mode",
    "rounds",
    "steps_done",
    "skipped_steps",
    "tool_calls",
    "strikes",
    "next_step_id",
    "reason",
)


def _as_int(value: Any) -> int:
    """宽容取整。快照是历史数据，读它的地方**不该因为一个坏字段就炸**。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── 数据形状 ────────────────────────────────────

@dataclass(frozen=True)
class Checkpoint:
    """一条内核检查点。字段与 ``kernel_checkpoints`` 表一一对应。"""

    conversation_id: int
    plan_id: int | None
    #: 计划内的步骤序号（1 起）。**给人读、给「序号单调不减」的断言用**，不参与定位。
    #: ``react`` 模式没有计划，恒为 0。
    step_index: int
    status: str
    snapshot: Mapping[str, Any] = field(default_factory=dict)
    id: int | None = None

    @classmethod
    def from_row(cls, row: Any) -> Checkpoint:
        """从 ORM 行构造。

        **走鸭子类型**（只取属性、不 import ``app.store``）：内核核心不反向依赖 store，
        这是既定分层。代价是这个方法接受不了「字段名不符」的行 ——
        而那正是单测该验的：字段名一旦对不上，这里就是第一个爆的地方。
        """
        return cls(
            conversation_id=int(row.conversation_id),
            plan_id=row.plan_id,
            step_index=_as_int(row.step_index),
            status=str(row.status),
            snapshot=dict(row.snapshot or {}),
            id=getattr(row, "id", None),
        )

    # ── 快照里的几个常用读数（都带兜底）────

    @property
    def next_step_id(self) -> str | None:
        """写这条检查点时记下的续跑点。空串与 None 同等对待 ——
        「没记」和「记了个空的」在恢复时要做的是同一件事。"""
        value = self.snapshot.get("next_step_id")
        return value if isinstance(value, str) and value else None

    @property
    def rounds(self) -> int:
        """上次运行停在第几轮。续跑的轮次从它接着数。"""
        return _as_int(self.snapshot.get("rounds"))

    @property
    def steps_done(self) -> int:
        return _as_int(self.snapshot.get("steps_done"))

    @property
    def tool_calls(self) -> int:
        return _as_int(self.snapshot.get("tool_calls"))

    @property
    def reason(self) -> str:
        value = self.snapshot.get("reason")
        return value if isinstance(value, str) else ""

    @property
    def strikes(self) -> dict[tuple[str, str], int]:
        return parse_strikes(self.snapshot.get("strikes"))

    @property
    def is_resumable(self) -> bool:
        return self.status in RESUMABLE_STATUSES


@dataclass(frozen=True)
class ResumeDecision:
    """「能不能续、从哪续」的结论。``resumable=False`` 时 ``reason`` 说明为什么。"""

    resumable: bool
    reason: str
    #: 续跑要落到哪一步（``PlanStep.id``）。``None`` = 没有计划可定位
    #: （``react`` 模式，或计划已全部收口）。
    start_step_id: str | None = None
    #: ``start_step_id`` 在计划里的序号（1 起）；0 表示无计划。
    step_index: int = 0
    #: 上次运行停在第几轮 —— 续跑要接着数，否则「跑了多少轮」会凭空缩水。
    rounds: int = 0
    steps_done: int = 0
    #: 检查点记的续跑点与计划现状不一致。**不阻止续跑**，只是让人知道发生过什么。
    stale: bool = False


# ── 纯函数 ──────────────────────────────────────

def next_step_id(plan: Plan | None) -> str | None:
    """计划里第一个「还没过去」的步骤 id；没有这样的步骤则 ``None``。

    判据用 ``status`` 而不是「位置」：``failed`` 的步骤仍然是续跑点 ——
    那一步没做成，续跑要重新面对它。这正是它与 ``skipped`` 的区别。
    """
    if plan is None:
        return None
    for step in plan.steps:
        if step.status not in SETTLED_STATUSES:
            return step.id
    return None


def step_index_of(plan: Plan | None, step_id: str | None) -> int:
    """``step_id`` 在计划里的序号（1 起）。

    - 无计划 → ``0``
    - ``step_id=None`` 而计划有步骤 → 计划长度（「全都过去了」）
    - ``step_id`` 不在计划里 → ``0``（找不到就是找不到，不瞎猜一个位置出来）
    """
    if plan is None:
        return 0
    if step_id is None:
        return len(plan.steps)
    for position, step in enumerate(plan.steps, start=1):
        if step.id == step_id:
            return position
    return 0


def dump_strikes(strikes: Mapping[tuple[str, str], int]) -> list[list[Any]]:
    """把自愈计数序列化成 JSON 能存下的形状。

    计数键是 ``(tool, args_hash)`` 元组，JSON 的对象键只能是字符串 ——
    硬塞的话得自己拼一个分隔符，而工具名里可能出现任何字符。用三元组列表最省事，
    也顺带让快照在人读时能看出「哪个工具的哪组参数失败了几次」。
    """
    return [[tool, digest, int(count)]
            for (tool, digest), count in sorted(strikes.items())]


def parse_strikes(raw: Any) -> dict[tuple[str, str], int]:
    """读回自愈计数。**坏数据一律丢弃，绝不抛**。

    快照是历史数据：格式演进过、或被人手改过都很正常。这条路径在恢复的关键路径上，
    为了几个字节的计数让它崩掉，代价是「整个会话续不了」；而丢掉计数最坏的后果
    只是少一层自愈保护 —— 那有 ``max_steps`` 兜着。
    """
    result: dict[tuple[str, str], int] = {}
    if not isinstance(raw, (list, tuple)):
        return result
    for item in raw:
        if not (isinstance(item, (list, tuple)) and len(item) == 3):
            continue
        tool, digest, count = item
        if not isinstance(tool, str) or not isinstance(digest, str):
            continue
        try:
            result[(tool, digest)] = int(count)
        except (TypeError, ValueError):
            continue
    return result


def make_snapshot(
    plan: Plan | None,
    *,
    mode: str,
    rounds: int,
    steps_done: int = 0,
    skipped_steps: int = 0,
    tool_calls: int = 0,
    strikes: Mapping[tuple[str, str], int] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """按约定拼一份快照。

    **写入方只准用它**，不要手写 dict：键名一旦各写各的，读的那一头就得同时兼容
    两套拼法，而这类分歧不会被任何测试发现（两边各自都跑得好好的），
    只会让某天的一次恢复读出 0 轮、0 步。

    ``next_step_id`` 不作为一个参数传进来，而是**从计划现算**：让调用方自己填，
    等于允许「计划说该跑第 4 步、快照记着第 2 步」这种自相矛盾的检查点写进库。
    """
    return {
        "mode": mode,
        "rounds": int(rounds),
        "steps_done": int(steps_done),
        "skipped_steps": int(skipped_steps),
        "tool_calls": int(tool_calls),
        "strikes": dump_strikes(strikes or {}),
        "next_step_id": next_step_id(plan),
        "reason": reason,
    }


def decide_resume(
    checkpoint: Checkpoint | None, plan: Plan | None = None,
) -> ResumeDecision:
    """「这个会话能不能接着跑、从哪接着跑」。

    三种不能续的情况，各有各的话要说 —— 它们对用户的下一步动作完全不同：
    「还没开始跑」「上次已经跑完了」「检查点坏了」。
    """
    if checkpoint is None:
        return ResumeDecision(
            False, "这个会话还没有任何检查点，没有可续跑的起点。",
        )

    if checkpoint.status == DONE:
        return ResumeDecision(
            False,
            "上一次运行已经跑完；再跑一次是重新开始，不是续跑。"
            "若要重做，请新开一轮对话。",
        )

    if checkpoint.status not in RESUMABLE_STATUSES:
        return ResumeDecision(
            False,
            f"检查点状态 {checkpoint.status!r} 不是已知的四种之一"
            f"（{'/'.join(VALID_STATUSES)}），无法判断该不该继续。",
        )

    rounds, steps_done = checkpoint.rounds, checkpoint.steps_done

    if plan is None or not plan.steps:
        # ``react`` 模式：本来就没有计划可定位，续跑点只能从快照读（实际上也没有），
        # 「接着跑」的全部含义就是「带着已跑的轮数继续问模型」。
        return ResumeDecision(
            True,
            f"没有生效中的计划（react 模式），按研究目标继续；"
            f"上次已跑 {rounds} 轮、完成 {steps_done} 步。",
            start_step_id=None, step_index=0, rounds=rounds, steps_done=steps_done,
        )

    live = next_step_id(plan)
    recorded = checkpoint.next_step_id
    stale = recorded != live
    where = step_index_of(plan, live)

    if stale:
        reason = (
            f"计划自上次检查点之后已被推进（检查点记的是 {recorded!r}，"
            f"现在该跑 {live!r}）；以计划为准继续。"
        )
    elif live is None:
        reason = "计划各步都已收口，续跑会直接收尾。"
    else:
        reason = f"从步骤 {live!r}（计划第 {where} 步）继续。"

    return ResumeDecision(
        True, reason, start_step_id=live, step_index=where,
        rounds=rounds, steps_done=steps_done, stale=stale,
    )


__all__ = [
    "CANCELLED",
    "DONE",
    "PAUSED",
    "RESUMABLE_STATUSES",
    "RUNNING",
    "SNAPSHOT_KEYS",
    "VALID_STATUSES",
    "Checkpoint",
    "ResumeDecision",
    "decide_resume",
    "dump_strikes",
    "make_snapshot",
    "next_step_id",
    "parse_strikes",
    "step_index_of",
]
