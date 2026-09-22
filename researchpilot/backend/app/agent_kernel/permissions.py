"""权限闸门（US-406，对齐设计 §10.1 与决策 D5）。

设计 §10.1 的两条硬约束决定了这个模块的全部形状：

1. **权限等级是工具的静态属性**（D5）。让模型在调用时自称「这次是只读的」等于没有分级 ——
   一个被提示注入污染的模型会立刻这样自称。所以这里读的是 ``ToolSpec.permission``，
   与模型说了什么无关。
2. **危险操作强制事前审批，不可配置关闭**。所以 ``dangerous`` 分支里没有「关闭审批」的
   开关，也没有「记住本次会话」的捷径：一次批准换永久放行，等于把审批疲劳变成绕过审批。

四档的处置（计划 §5「权限闸门」）：

| 等级 | 行为 | 记忆 |
|---|---|---|
| ``read`` | 直通 | — |
| ``write`` | 直通（写前快照在第 7 步的检查点里） | — |
| ``execute`` | **首次**人工批准 | 批准后按作用域记住 |
| ``dangerous`` | **每次**都必须批准 | **无记忆**，刻意如此 |

⚠️ **关于「按目录记忆」（D5）**：机制保留在 ``grant_key`` 里（``tool_grant:<tool>[:<scope>]``），
但**当前没有一个 execute 工具声明 scope** —— `run_pipeline` 没有目录参数，硬给它造一个
只会变成没人消费的字段。等第 9 步加进有目录语义的 execute 工具时，`ToolSpec.scope_arg`
一填就生效，不需要改这里的判定。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from app.agent_kernel.tools.base import (
    DANGEROUS,
    EXECUTE,
    READ,
    WRITE,
)

#: 审批记忆在 ``app_config`` 里的键前缀。落库才能跨重启有效（D5）—— 只记在进程内存里的话，
#: 用户批准过的动作会在重启后重新要一遍批准，而「刚批过又要批」正是审批疲劳的来源。
GRANT_PREFIX = "tool_grant:"

#: 权限类审批单的 ``approvals.kind``（D4：复用 Sprint 3 的审批表而不是另起一张）。
#: 与既有的 ``budget`` 并列 —— 前端按 kind 决定审批卡显示什么、批准语义是什么。
APPROVAL_KIND_DANGEROUS = "dangerous"


def grant_key(tool_name: str, scope: str | None = None) -> str:
    """审批记忆的键。

    ``scope`` 为 ``None`` 时是**工具级**记忆（整个工具批准一次）；给了 scope 则是
    作用域级（例如某个目录）。两种键不互相命中 —— 目录级的批准不该让同一工具
    在别的目录上也免批。
    """
    tool = tool_name.strip()
    if not tool:
        raise ValueError("grant_key 需要工具名（它是审批记忆与审计的公共键）")
    if scope is None or not str(scope).strip():
        return f"{GRANT_PREFIX}{tool}"
    return f"{GRANT_PREFIX}{tool}:{str(scope).strip()}"


def scope_of(args: dict[str, Any], scope_arg: str | None) -> str | None:
    """从调用参数里取作用域值。工具没声明 ``scope_arg`` → 工具级记忆。"""
    if not scope_arg:
        return None
    value = args.get(scope_arg)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class Decision:
    """一次调用的闸门判定。**冻结**：判定结果要能原样进审计与审批单。"""

    needs_approval: bool
    reason: str
    #: 批准后应当签发的记忆键；``dangerous`` 恒为 ``None``（没有记忆可签）
    grant_key: str | None = None

    @property
    def granted(self) -> bool:
        return not self.needs_approval


#: 判定结果里出现的那句话，同时进审计、审批单与事件 —— 三处用同一句话，
#: 用户在界面上看到的理由与事后翻到的理由才是同一条。
REASON_READ = "只读工具，直通"
REASON_WRITE = "写草稿工具，直通（写前快照见第 7 步）"
REASON_EXECUTE_FIRST = "执行类工具需首次人工批准"
REASON_EXECUTE_REMEMBERED = "该执行类工具已获批准（按作用域记忆）"
REASON_DANGEROUS = "危险操作每次都必须人工批准（设计 §10.1，不可配置关闭）"


def authorize(
    permission: str,
    *,
    tool_name: str,
    granted: Collection[str] = (),
    scope: str | None = None,
) -> Decision:
    """判定一次调用要不要人工批准。

    ``granted`` 是**已生效的记忆键集合**（由 store 提供，来自 ``app_config``）。
    传进来而不是在这里查库：闸门保持纯函数，判定就能脱库单测 —— 而「哪些动作需要批准」
    恰恰是最需要被逐档断言、又最不该依赖数据库状态的东西。
    """
    if permission == READ:
        return Decision(needs_approval=False, reason=REASON_READ)
    if permission == WRITE:
        return Decision(needs_approval=False, reason=REASON_WRITE)
    if permission == EXECUTE:
        key = grant_key(tool_name, scope)
        if key in set(granted):
            return Decision(
                needs_approval=False,
                reason=f"{REASON_EXECUTE_REMEMBERED}（{key}）",
                grant_key=key,
            )
        return Decision(
            needs_approval=True,
            reason=f"{REASON_EXECUTE_FIRST}（{tool_name}）",
            grant_key=key,
        )
    if permission == DANGEROUS:
        # 刻意不看 granted：`dangerous` 的记忆如果有，就是「一次批准换永久放行」。
        # 这里连查询都不做，避免将来有人「顺手」把它接上。
        return Decision(needs_approval=True, reason=f"{REASON_DANGEROUS}（{tool_name}）")

    # 未知等级**拒绝而不是放行**。`unknown_tools` 那种加载期问题只告警，是因为它不会
    # 造成副作用；而这里是「马上要执行一个能力不明的工具」，默认放行等于没有权限体系。
    return Decision(
        needs_approval=True,
        reason=f"未知权限等级 {permission!r}，按最严处理（{tool_name}）",
    )


@dataclass(frozen=True)
class PendingCall:
    """一条**已过闸门判定**的调用。要被 JSON 序列化进 ``approvals.detail``。

    所以字段全是基本类型，且用 ``call_id`` 而不是对象引用 —— 审批单可能在另一个进程、
    另一天被处理，任何内存引用都活不到那一刻。

    ⚠️ ``needs_approval=False`` 的那些**也在里面**。挂起的是一整轮：
    assistant 那条消息里的 ``tool_calls`` 是一个整体，恢复时整轮重放才能把
    「调用 ↔ 结果」配齐。只带需要批准的那几条，恢复后那一轮的配对就永远差几条，
    下一轮请求会被端点直接拒收。
    """

    call_id: str
    tool_name: str
    args: dict[str, Any]
    permission: str
    reason: str
    grant_key: str | None = None
    #: 这一条是否需要人工批准。同一轮里可能既有 ``run_command``（要批）
    #: 又有 ``read_file``（直通）—— 界面上必须能分开显示，否则用户会以为
    #: 「读一个文件也需要批准」。
    needs_approval: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool": self.tool_name,
            "args": dict(self.args),
            "permission": self.permission,
            "reason": self.reason,
            "grant_key": self.grant_key,
            "needs_approval": self.needs_approval,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PendingCall:
        return cls(
            call_id=str(raw.get("call_id") or ""),
            tool_name=str(raw.get("tool") or ""),
            args=dict(raw.get("args") or {}),
            permission=str(raw.get("permission") or ""),
            reason=str(raw.get("reason") or ""),
            grant_key=raw.get("grant_key") or None,
            # 缺省按**要批**处理：反序列化一份老数据（没有这个键）时，
            # 宁可多重问人一次，也不要默默放行一条没记清是否需要批准的调用。
            needs_approval=bool(raw.get("needs_approval", True)),
        )


class ApprovalRequired(Exception):
    """本轮执行需要人工批准，整轮挂起。

    **它不是错误**，与预算熔断同一档：`run()` 会把它转成 ``paused`` 并保留现场，
    而不是落成 ``failed``。作为独立类型（而非 ``KernelError`` 子类）是刻意的 ——
    若它带 ``code``，那条「一切 ``KernelError`` 都是失败」的兜底逻辑会把它顺手变成失败，
    而人还没机会看到审批单。

    为什么**整轮**挂起而不是「先跑可放行的、把待批的留下」：assistant 那条消息里的
    ``tool_calls`` 是一个整体，只回填一半会让下一轮请求里出现「有调用没有结果」的配对，
    端点会拒收；而且「哪些跑过、哪些没跑」在恢复时就成了第 7 步检查点才回答得了的问题。
    """

    def __init__(self, round_calls: tuple[PendingCall, ...], *, reason: str = "") -> None:
        self.round_calls = tuple(round_calls)
        gated = tuple(call for call in self.round_calls if call.needs_approval)
        if not gated:
            # 构造出一个「什么都不用批」的挂起，只可能来自调用点的逻辑错误。
            # 放它过去的话，本轮会静默地一条都不执行（``_gate`` 已经 return 过了），
            # 表现为「作业暂停了，但审批单里没有待批项」。
            raise ValueError("ApprovalRequired 至少要有一条 needs_approval=True 的调用")
        self.gated = gated
        self.reason = reason or "；".join(
            dict.fromkeys(call.reason for call in gated)
        ) or "需要人工批准"
        super().__init__(self.reason)

    @property
    def pending(self) -> tuple[PendingCall, ...]:
        """需要批准的那些（界面上的审批项）。整轮的其余调用见 ``round_calls``。"""
        return self.gated

    def detail(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "pending": [call.to_dict() for call in self.gated],
            "round": [call.to_dict() for call in self.round_calls],
        }
