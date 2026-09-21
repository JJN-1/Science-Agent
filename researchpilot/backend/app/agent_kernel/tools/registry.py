"""工具注册表（US-404）。

注册表是**「模型说要调什么」与「系统真的调了什么」之间唯一的收口**。它负责四件事，
每一件都对应一个具体的失败模式：

| 职责 | 不这么做会怎样 |
|---|---|
| 重名拒绝 | 后注册的悄悄顶掉先注册的，调用点拿到的是另一个工具 |
| 白名单校验（§5.3） | 任何 Agent 都能调任何工具，「最小权限」只剩一句注释 |
| 参数 schema 校验 | 工具带着模型瞎编的参数跑起来，用默认值执行了另一件事 |
| 结果截断（D10） | 一条 5 MB 的输出把上下文吃光，模型看不到后面的东西 |

计时也在这里做：``ToolResult.duration_ms`` 是界面「工具调用卡片」上唯一的耗时来源，
放在各工具自己里面算，迟早会出现「有的工具没记」。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import Any

from app.agent_kernel.errors import ToolError
from app.agent_kernel.tools.base import (
    Tool,
    ToolContext,
    ToolResult,
    ToolSpec,
    truncate_tool_output,
)
from app.ai.base import monotonic
from app.ai.schema_utils import schema_errors

#: 子码常量：调用方要按它分支（403 / 400 的处置不同），不要靠比对消息文本。
NOT_REGISTERED = "AGENT-TOOL-001"
NOT_ALLOWED = "AGENT-TOOL-002"
BAD_ARGUMENTS = "AGENT-TOOL-003"


class ToolRegistry:
    """已注册工具的只读视图 + 调用入口。进程内对象，不落库。

    工具是**代码资产**：它们的 schema、权限等级、超时都是实现的一部分，改一个值要过测试。
    因此这里没有 ``unregister`` —— 能热拔插的工具集会让「这次为什么没调到那个工具」
    变成一个需要翻运行日志才能回答的问题。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ── 装配 ────────────────────────────────────
    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ToolError(
                f"工具名重复：{name}（已注册 {type(self._tools[name]).__name__}，"
                f"又来 {type(tool).__name__}）—— 静默覆盖会让调用点拿到另一个工具",
                code=NOT_REGISTERED,
            )
        self._tools[name] = tool

    # ── 查询 ────────────────────────────────────
    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            known = "、".join(self.names()) or "（空）"
            raise ToolError(f"工具未注册：{name}；已注册：{known}", code=NOT_REGISTERED)
        return tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec for name in self.names()]

    def describe(self) -> list[dict[str, Any]]:
        """``GET /api/tools`` 的数据源。返回的是 ``spec.to_dict()``，**不含**谁有资格调用它 ——
        白名单是 ``AgentSpec`` 的属性，由 API 层合并（见 ``app.api.tools``）。"""
        return [spec.to_dict() for spec in self.specs()]

    def tool_definitions(self, allowed: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """组装给模型的 ``tools`` 参数。

        ``allowed`` 给 ``None`` 表示不按白名单过滤（用于「看全部工具」的管理视图）；
        传一个集合就只出集合内的。**过滤在这里做而不是在提示词里写**：写在提示词里，
        模型仍可能调一个没列出的工具，而那一层没有任何东西会拦它。
        """
        if allowed is None:
            names = self.names()
        else:
            keep = set(allowed)
            names = [name for name in self.names() if name in keep]
        return [self._tools[name].spec.to_tool_definition() for name in names]

    # ── 校验 ────────────────────────────────────
    def ensure_allowed(self, name: str, allowed: Collection[str] | None) -> None:
        """白名单校验（§5.3 最小权限）。``None`` = 不做白名单判断。"""
        if allowed is None:
            return
        if name not in set(allowed):
            listed = "、".join(sorted(allowed)) or "（无）"
            raise ToolError(
                f"工具 {name} 不在调用方白名单内；该调用方的白名单：{listed}",
                code=NOT_ALLOWED,
            )

    def validate_args(self, name: str, args: dict[str, Any]) -> None:
        """按工具自己的 schema 校验参数。

        违规路径**原样带进错误信息**（``$.stage_ids[0]``）：只说「参数不合法」，
        调用方（第 5 步是模型，带重试）只能靠猜 —— 与 ``LLM-SCHEMA-001`` 同一条处置原则。
        """
        spec = self.get(name).spec
        if not isinstance(args, dict):
            raise ToolError(
                f"工具 {name} 的参数必须是对象，实际是 {type(args).__name__}",
                code=BAD_ARGUMENTS,
            )
        errors = schema_errors(args, spec.parameters)
        if errors:
            raise ToolError(
                f"工具 {name} 的调用参数不符合 schema：{'；'.join(errors)}",
                code=BAD_ARGUMENTS,
            )

    # ── 调用 ────────────────────────────────────
    def invoke(
        self,
        name: str,
        args: dict[str, Any] | None,
        ctx: ToolContext,
        *,
        allowed: Collection[str] | None = None,
    ) -> ToolResult:
        """校验 → 执行 → 计时 → 截断。这是内核里**唯一**触发工具副作用的入口。

        - 白名单与参数问题抛 ``ToolError``（在工具跑起来之前，没有副作用）
        - 工具自己抛的其它异常兜成 ``ToolResult(ok=False)``：一个工具的 bug 不该掀掉
          整个作业，而且模型需要看到失败原因才有机会换参数（D9）
        """
        tool = self.get(name)
        self.ensure_allowed(name, allowed)
        payload = args or {}
        self.validate_args(name, payload)

        started = monotonic()
        try:
            result = tool.run(payload, ctx)
        except ToolError:
            # 工具内部判定的契约违规（例如它自己再校验一次参数）—— 不该被吞成 ok=False
            raise
        except Exception as exc:  # noqa: BLE001 —— 兜底是刻意的，见 docstring
            result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        result.duration_ms = int((monotonic() - started) * 1000)

        output, truncated = truncate_tool_output(result.output, tool.spec.result_max_bytes)
        result.output = output
        result.truncated = truncated
        return result
