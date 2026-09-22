"""内核工具清单的**唯一装配点**（US-406）。

在它出现之前，「有哪些工具」这件事有三个消费者：``main.py`` 的装配、``GET /api/tools``
的数据源、以及 ``test_agent_specs`` 里那条「白名单里写了、注册表里没有」的断言。
三处各写一遍注册代码，漂移的表现是**测试用一套工具、生产跑另一套** ——
而两边各自都是绿的。

``run_pipeline`` 的编排函数仍然是**注入**的（内核不 import 编排层，见 ``tools.pipeline``），
``SandboxPolicy`` 也是：它们是装配期的输入，不是这个模块能自己决定的东西。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.agent_kernel.sandbox import SandboxPolicy
from app.agent_kernel.tools.base import Tool
from app.agent_kernel.tools.fs import (
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from app.agent_kernel.tools.pipeline import PipelineRunner, RunPipelineTool
from app.agent_kernel.tools.registry import ToolRegistry
from app.agent_kernel.tools.shell import RunCommandTool

#: 沙箱工具的**规范名单**。``AgentSpec.tools`` 的白名单、负向断言（「只有 executor
#: 拿得到」）与 ``GET /api/sandbox`` 的说明都引它，避免三处各自列一遍。
SANDBOX_TOOL_NAMES: tuple[str, ...] = (
    "read_file", "write_file", "list_dir", "glob", "grep", "run_command",
)

#: 本版**不提供**的工具。``http_get`` 在 Sprint 4 的规划里出现过，D6 的裁定是不做：
#: 网络隔离在 Windows 上没有轻量实现，与其提供一个「能出网但管不住」的工具，
#: 不如一个都不给 —— 暴露面靠「没有工具」缩小，而不是靠「工具会自觉」。
WITHHELD_TOOL_NAMES: tuple[str, ...] = ("http_get",)


def build_tools(
    *,
    runner: PipelineRunner,
    stage_ids: Sequence[str],
    policy: SandboxPolicy,
) -> tuple[Tool, ...]:
    """构造全部内核工具。返回 ``tuple`` 而不是 ``list``：这是一份固定清单，
    调用方顺手 append 一个进去，注册表里就会多出一个没人审计过的工具。"""
    return (
        RunPipelineTool(runner=runner, stage_ids=stage_ids),
        ReadFileTool(policy=policy),
        WriteFileTool(policy=policy),
        ListDirTool(policy=policy),
        GlobTool(policy=policy),
        GrepTool(policy=policy),
        RunCommandTool(policy=policy),
    )


def build_registry(
    *,
    runner: PipelineRunner,
    stage_ids: Sequence[str],
    policy: SandboxPolicy,
) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in build_tools(runner=runner, stage_ids=stage_ids, policy=policy):
        registry.register(tool)
    return registry


__all__ = [
    "SANDBOX_TOOL_NAMES",
    "WITHHELD_TOOL_NAMES",
    "build_registry",
    "build_tools",
]
