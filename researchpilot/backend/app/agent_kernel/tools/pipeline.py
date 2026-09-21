"""``run_pipeline`` —— 把 S1–S8 确定性编排注册成内核的一个能力（US-404 / D1）。

设计 §4.1 对内核层的描述是：「对话式入口；**S1–S8 确定性编排作为其一种技能**」。
本文件就是那句话的落地：编排仍是 ``Orchestrator`` 的事（状态机、回退边、检查点都归它），
内核这边只提供**一个入口**，让模型能说「先把文献综述那一段跑了」。

⚠️ **编排函数是注入的，不在这里 import**。原因不是洁癖：``app.orchestration`` 本来就
依赖内核（编排层调用内核的能力），内核再反过来 import 它，就出现环。注入之后这个文件
只认识一个 ``Callable``，而「内核能不能脱库/脱编排单测」也顺带成立了。

⚠️ **``project_id`` 不从 ``arguments`` 里取**。它由 ``ToolContext`` 注入，模型无从指定 ——
越权入口往往不是权限判断写错了，而是「这个值本来就不该由调用方给」。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.agent_kernel.tools.base import (
    EXECUTE,
    Tool,
    ToolContext,
    ToolResult,
    ToolSpec,
)

#: 编排器 ``run_pipeline`` 的形状。刻意用 ``Callable`` 而不是 import 真类型：
#: 内核不认识编排层，只认识这个方法签名。
PipelineRunner = Callable[..., list[int]]


class RunPipelineTool(Tool):
    """按给定阶段顺序推进确定性编排。"""

    def __init__(self, *, runner: PipelineRunner, stage_ids: Sequence[str]) -> None:
        ids = tuple(str(s).strip() for s in stage_ids if str(s).strip())
        if not ids:
            raise ValueError("run_pipeline 需要至少一个可用阶段（stage_ids 不能为空）")
        self._runner = runner
        self._stage_ids = ids
        self.spec = ToolSpec(
            name="run_pipeline",
            description=(
                "按顺序运行科研全链路阶段（S1 选题 → S8 投稿）。"
                "每个阶段产出结构化产物写入黑板；阶段本身不再返回内容，"
                "要看结果请另行读取黑板。任一阶段因预算熔断暂停时，后续阶段不再执行。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "stage_ids": {
                        "type": "array",
                        "minItems": 1,
                        # 枚举随可用阶段生成 —— 写死在这里会和 STAGE_ORDER 漂移，
                        # 而漂移的表现是「模型调了一个永远不存在的阶段」。
                        "items": {"type": "string", "enum": list(ids)},
                        "description": "要运行的阶段，按给定顺序执行。",
                    },
                },
                # **必填**：留空即「跑完全链路」这个默认值太贵（8 个阶段、真金白银），
                # 让调用方显式列出它到底要哪几步。
                "required": ["stage_ids"],
            },
            permission=EXECUTE,
            # 跑两次 = 两轮真实开销 + 两批 run 记录。非幂等工具**不允许**自动重试。
            idempotent=False,
            # 一个阶段动辄几分钟；这条超时是给调度层的上界，不是「预期耗时」。
            timeout_s=900.0,
        )

    @property
    def available_stage_ids(self) -> tuple[str, ...]:
        return self._stage_ids

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        requested = list(args["stage_ids"])
        if ctx.project_id is None:
            # 编排是项目内的动作。没有项目就调用它，说明装配错了，报错好过瞎跑。
            return ToolResult(
                ok=False,
                error="run_pipeline 缺少 project_id（应由 ToolContext 注入，不由模型提供）",
            )
        run_ids = self._runner(
            ctx.session, ctx.project_id, stage_ids=requested, job_id=ctx.job_id,
        )
        stopped_early = len(run_ids) != len(requested)
        output: dict[str, Any] = {
            "requested": requested,
            "run_ids": list(run_ids),
            "stopped_early": stopped_early,
        }
        if stopped_early:
            # 只报事实 + 两种可能原因，不猜是哪一种：猜错比不说更糟。
            output["note"] = (
                f"请求 {len(requested)} 个阶段，实际产生 {len(run_ids)} 条运行记录。"
                "可能原因：某个阶段未注册被跳过，或中途暂停（预算熔断待人工批准）。"
            )
        return ToolResult(ok=True, output=output)
