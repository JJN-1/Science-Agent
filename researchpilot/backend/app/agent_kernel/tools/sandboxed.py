"""沙箱工具的公共基类（US-406）。

``fs`` 与 ``shell`` 里的每个工具都要做同样三件事：拿到项目的沙箱根、把模型给的路径
解析到根内、把结果落成**相对**路径。做三遍的后果不是重复，而是**漏做一遍** ——
少一次 ``resolve_within`` 就是一个越界读写入口，而它在正常路径上完全看不出来。

``policy`` 走 ``__init__`` 注入而不是放进 ``ToolContext``：沙箱根来自数据目录，
是**装配期**的事实，一次装配终生有效；``ToolContext`` 装的是**每次调用**的定位信息
（哪个项目、哪个会话）。把装配期的东西塞进调用期，会让「谁能改沙箱根」变成一个
需要翻调用链才能回答的问题。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent_kernel.sandbox import (
    SandboxPolicy,
    SandboxViolation,
    relative_to_project,
    resolve_within,
)
from app.agent_kernel.tools.base import READ, Tool, ToolContext

#: 结果信封（``{path, sha256, content, ...}``）本身的字节余量。
#: ``result_max_bytes`` 若刚好等于内容上限，信封的键名就会把内容挤掉几个字节 ——
#: 表现为「明明设了 64 KB 却只拿到 63.9 KB」，而且只在边界上偶发。
SANDBOX_RESULT_SLACK = 4096


class SandboxedTool(Tool):
    """所有沙箱工具的基类：只提供「路径必须在项目目录内」这一件事。"""

    #: 子类各自的权限等级（``READ`` / ``WRITE`` / ``DANGEROUS``）
    permission: str = READ

    def __init__(self, *, policy: SandboxPolicy) -> None:
        self.policy = policy

    # ── 给子类用的四个动作 ──────────────────────

    def project_dir(self, ctx: ToolContext) -> Path:
        """项目的沙箱根。**不创建**（创建只在真要写入时发生，见 ``SandboxPolicy``）。"""
        project_id = self._project_id(ctx)
        return self.policy.project_dir(project_id)

    def resolve(self, ctx: ToolContext, raw: Any, **kwargs: Any) -> Path:
        """把模型给的路径解析到沙箱内；越界抛 ``SandboxViolation``。"""
        return resolve_within(self.policy, self._project_id(ctx), raw, **kwargs)

    def relative(self, ctx: ToolContext, path: Path) -> str:
        return relative_to_project(self.policy, self._project_id(ctx), path)

    def result_budget(self) -> int:
        """本工具结果的字节上限：内容上限 + 信封余量。"""
        return self.policy.max_output_bytes + SANDBOX_RESULT_SLACK

    # ── 内部 ────────────────────────────────────

    @staticmethod
    def _project_id(ctx: ToolContext) -> int:
        if ctx.project_id is None:
            # 与 ``run_pipeline`` 同一条约定：``project_id`` 只由 ``ToolContext`` 注入。
            # 模型若能自己指定项目号，沙箱的目录白名单就形同虚设 ——
            # 它只要传别的项目号就能读写别人的产物。
            raise SandboxViolation(
                "沙箱工具缺少 project_id（应由 ToolContext 注入，不由模型提供）"
            )
        return int(ctx.project_id)
