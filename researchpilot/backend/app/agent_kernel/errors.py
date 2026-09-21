from __future__ import annotations


class KernelError(RuntimeError):
    """内核错误基类。

    错误码格式沿用设计 §11.3 ``<域>-<类别>-<序号>``，域取 ``AGENT``。
    码写在消息最前面（与 ``LLM-SCHEMA-001`` 的既有写法一致），
    这样它随 ``str(exc)`` 一起进 ``job_events`` 与 ``llm_usage.error``，
    排查时不用再去翻日志找对应关系。
    """

    code = "AGENT-KERNEL-000"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}" if not message.startswith(self.code) else message)


class ContextBudgetError(KernelError):
    """上下文预算无法满足（系统提示本身就超窗）。

    这是内核**唯一**应该硬失败的情形：裁剪梯队的最后一步是丢掉当前计划，
    再往下就没有可丢的东西了 —— 剩下的只有「系统提示 + 用户这一句」。
    若连它都装不下，继续调用只会得到上游的 400，不如在本地说清楚发生了什么。
    """

    code = "AGENT-CTX-001"


class PlanError(KernelError):
    """计划内容或状态迁移非法（US-403）。

    子码：

    - ``AGENT-PLAN-001`` 计划内容非法（缺 id/title、id 重复、模式未知、空步骤）
    - ``AGENT-PLAN-002`` 确定性模式与编排模式冲突（D7 规定 deterministic 恒为 plan_execute）
    - ``AGENT-PLAN-003`` 违反编辑规则（确定性计划不许改步骤 / 已批准的不能再改）

    单独分类而不是复用 001 的原因：调用方对这三类的处置完全不同 ——
    001 是「模型给的东西不能用，重试或回退」，002 是「用户传参自相矛盾」，
    003 是「这次操作本身就不该发生」。混成一个码，前端只能笼统提示。
    """

    code = "AGENT-PLAN-001"
