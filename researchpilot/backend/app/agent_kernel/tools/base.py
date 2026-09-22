"""工具契约：``ToolSpec`` / ``ToolResult`` / ``Tool``（US-404）。

对齐设计 **§5.3 `AgentSpec`**（工具自报的元数据）与 **§10.1 工具权限分级**。

三个刻意的取舍：

1. **权限等级是工具的静态属性**（D5）。让模型在调用时自称「这次是只读的」，
   等于没有权限分级 —— 一个被提示注入污染的模型会立刻这样自称。
2. **结果超限时头尾保留 + 写明丢了多少**（D10）。静默截断等于对模型撒谎：
   它会在「结果就这么长」的前提下继续推理，而结论建立在缺失的数据上。
3. **工具名是身份**：``ToolSpec.name`` 会被写进 ``tool_calls.tool_name``、事件、
   白名单与提示词，是事后唯一能对齐「模型说要调什么」与「系统真的调了什么」的键。
   注册后改名等于换了一个工具，不是同一个工具的别名。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.agent_kernel.errors import ToolError

# ── 权限分级（§10.1）────────────────────────────
#: 只读：不改任何东西。
READ = "read"
#: 写草稿：产出可回滚的中间物。
WRITE = "write"
#: 执行：跑命令、跑实验 —— 有副作用，但还没到「删数据/对外发送」。
EXECUTE = "execute"
#: 危险：删除、覆盖、出网写、越出白名单。§10.1 规定**每次都要批准，不可配置关闭**。
DANGEROUS = "dangerous"

PERMISSIONS: tuple[str, ...] = (READ, WRITE, EXECUTE, DANGEROUS)

#: 单工具结果上限（D10）。32 KB 是「足够长的一条命令输出」与「把上下文吃掉一半」的分界。
DEFAULT_RESULT_MAX_BYTES = 32 * 1024

#: 截断标记预留的字节额度。标记形如 ``\\n…[已截断 12345 字节]…\\n``，
#: 即使 dropped 有 19 位也放得下；先扣掉它，最后的长度才不会超过上限。
_TRUNCATION_RESERVE = 64
#: 头尾分配：头多尾少。尾部通常是错误栈/总结，头通常是主体。
_HEAD_RATIO = 0.6


@dataclass(frozen=True)
class ToolSpec:
    """一次工具调用的完整契约。**冻结**：注册之后它就不该再变。

    ``parameters`` 是 JSON Schema，复用 ``app.ai.schema_utils`` 校验（不引 jsonschema）。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    permission: str = READ
    #: 幂等 = 同样的参数再来一次不产生新的副作用。调度层据此决定失败后能否自动重试：
    #: 对非幂等工具自动重试，会把「一次提交」变成「两次提交」。
    idempotent: bool = True
    timeout_s: float = 30.0
    result_max_bytes: int = DEFAULT_RESULT_MAX_BYTES
    #: ``execute`` 类工具的**审批记忆作用域**参数名（D5）。
    #:
    #: 填了它，「批准一次」只对该参数值生效（例如某个目录）；留空则记忆粒度是整个工具。
    #: 当前**没有任何工具填它** —— 唯一的 execute 工具 ``run_pipeline`` 没有目录语义，
    #: 给它硬造一个只会变成没人消费的字段。第 9 步加进有目录语义的 execute 工具时，
    #: 这里填上参数名即可生效，``permissions.authorize`` 不需要改。
    #:
    #: ⚠️ 它**不进** ``to_tool_definition()``：那是给模型的协议字段，多一个键会让部分
    #: 端点直接 400，而它本来就是给闸门看的、不是给模型看的。
    scope_arg: str | None = None

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise ToolError("工具名不能为空（它是白名单、事件与审计的公共键）")
        if name != self.name:
            object.__setattr__(self, "name", name)
        if not self.description.strip():
            raise ToolError(f"工具 {name} 缺少 description（模型只靠它决定要不要调用）")
        if not isinstance(self.parameters, dict):
            raise ToolError(f"工具 {name} 的 parameters 必须是 JSON Schema 对象")
        if self.permission not in PERMISSIONS:
            raise ToolError(
                f"工具 {name} 的权限等级非法：{self.permission}"
                f"（可选 {'、'.join(PERMISSIONS)}）"
            )
        if self.timeout_s <= 0:
            raise ToolError(f"工具 {name} 的 timeout_s 必须为正，实际 {self.timeout_s}")
        if self.result_max_bytes <= 0:
            raise ToolError(
                f"工具 {name} 的 result_max_bytes 必须为正，实际 {self.result_max_bytes}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
            "permission": self.permission,
            "idempotent": self.idempotent,
            "timeout_s": self.timeout_s,
            "result_max_bytes": self.result_max_bytes,
        }

    def to_tool_definition(self) -> dict[str, Any]:
        """转成 OpenAI 协议的 ``tools`` 元素（第 5 步把它交给 ``ChatRequest.tools``）。

        **只放协议认识的三样**（name / description / parameters）：额外字段会让部分端点
        直接 400，而 name+description+parameters 已经是模型能看到的全部。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass
class ToolResult:
    """一次工具调用的结算结果。

    ``ok=False`` 表示**工具跑了但失败了**（D9 的错误自愈按 ``(tool, args_hash)`` 计数
    就建立在这个字段上）。它刻意不与「调用方违规」共用通道 —— 后者抛 ``ToolError``。
    """

    ok: bool = True
    output: Any = None
    error: str = ""
    duration_ms: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
        }


@dataclass
class ToolContext:
    """工具执行时的环境。**由内核注入，不从模型给的参数里取。**

    ``project_id`` 之类的东西若能由模型在 ``arguments`` 里指定，模型就等于能跨项目读写 ——
    越权入口通常不是权限判断写错了，而是「这个值本来就不该由调用方给」。
    """

    #: SQLAlchemy Session。注释成 ``Any`` 是刻意的：内核层不 import ``app.store``，
    #: 走鸭子类型（与 ``Plan.from_row`` 同一条约定），换取内核可脱库单测。
    session: Any = None
    project_id: int | None = None
    job_id: int | None = None
    agent_id: str | None = None
    conversation_id: int | None = None


class Tool(ABC):
    """一个内核能力。

    ``spec`` 由实现方在 ``__init__`` 里赋值（``run_pipeline`` 的 stage 清单是注入的，
    所以它不能是类常量），注册表只读它、不构造它。
    """

    spec: ToolSpec

    @property
    def name(self) -> str:
        return self.spec.name

    @abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行。参数已由注册表按 ``spec.parameters`` 校验过。

        失败的表达方式有两种，别混：

        - **业务失败** → 返回 ``ToolResult(ok=False, error=...)``，模型看得见并可换参数重试
        - **调用方违规** → 抛 ``ToolError``（不该靠模型去修白名单或 schema）

        未捕获的其它异常由注册表兜底成 ``ok=False``，不让一个工具的 bug 掀掉整个作业。
        """


def _take_head(text: str, byte_budget: int) -> tuple[str, int]:
    """从头取到不超过 ``byte_budget`` 字节，且在字符边界切断（多字节字符不劈开）。"""
    out: list[str] = []
    used = 0
    for ch in text:
        size = len(ch.encode("utf-8"))
        if used + size > byte_budget:
            break
        out.append(ch)
        used += size
    return "".join(out), used


def _take_tail(text: str, byte_budget: int) -> tuple[str, int]:
    out: list[str] = []
    used = 0
    for ch in reversed(text):
        size = len(ch.encode("utf-8"))
        if used + size > byte_budget:
            break
        out.append(ch)
        used += size
    return "".join(reversed(out)), used


def truncate_tool_output(payload: Any, max_bytes: int) -> tuple[Any, bool]:
    """把工具结果压到 ``max_bytes`` 以内，返回 ``(结果, 是否截断)``。

    未超限时**原样返回原对象**（不顺手 JSON 化）：调用点拿到的仍是它自己的结构，
    可以继续按结构用；只有真的超限了才退化成字符串 —— 此时保真已经不可能，
    唯一还能保证的是「如实告诉模型少看了多少」。

    超限时保留头 60% / 尾 40%，中间插入截断标记。**头尾都留**是因为结论常在尾部
    （错误栈、汇总行），而主体在头部，只留一头会让模型看到半截因果。
    """
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str
    )
    total = len(text.encode("utf-8"))
    if total <= max_bytes:
        return payload, False

    reserve = _TRUNCATION_RESERVE if max_bytes > 2 * _TRUNCATION_RESERVE else max_bytes // 2
    budget = max_bytes - reserve
    head_budget = int(budget * _HEAD_RATIO)
    head, used_head = _take_head(text, head_budget)
    tail, used_tail = _take_tail(text, budget - head_budget)
    dropped = total - used_head - used_tail
    marker = f"\n…[已截断 {dropped} 字节]…\n"
    return f"{head}{marker}{tail}", True
