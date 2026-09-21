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


class ToolError(KernelError):
    """工具契约、白名单或调用参数不合法（US-404）。

    子码：

    - ``AGENT-TOOL-001`` 工具契约非法（名字为空、权限等级未知、超时/上限非正）或名称重复、未注册
    - ``AGENT-TOOL-002`` 工具不在调用方的白名单内（§5.3 最小权限）
    - ``AGENT-TOOL-003`` 调用参数不符合该工具的 JSON Schema

    这三类都**在工具真正跑起来之前**抛出，与「工具跑了但失败了」严格分开：后者是
    ``ToolResult(ok=False)``（模型看得见、可以换参数重试，见 D9），前者是调用方
    自己写错了 —— 把它也做成 ok=False，等于让模型去「修」一个它无权修改的白名单。
    """

    code = "AGENT-TOOL-001"


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


class StepFailureError(KernelError):
    """同一步骤同一组参数连续失败到上限，该步终止并转人工（US-405 / D9）。

    计数键是 ``(tool, args_hash)`` 而**不是**工具名：按工具名计会把「换个参数重试」
    也算进失败 —— 而那本来就是模型该做的正常操作。反过来，只有「同样的调用连着失败
    N 次」才说明模型陷入了循环：它在用同一份输入期待不同的输出。

    这里**不降级、不跳过**：跳过等于让任务带着一个已知没做成的前提继续往下跑，
    而后续步骤会基于缺失的结果继续推理 —— 那比停下来更贵。
    """

    code = "AGENT-STEP-001"


class LoopLimitError(KernelError):
    """达到循环步数上限仍未收敛（US-405）。

    ReAct 的终止条件是「模型不再调用工具」。模型若一直调下去，没有上限就是一个
    不会返回的循环 —— 每一轮都真花一次模型调用的钱。

    与预算熔断（``LLM-BUDGET-001`` → 暂停 + 审批）刻意分开：预算熔断是**钱的问题**，
    加钱就能继续；步数上限是**模型没收敛**，加钱只会让它多绕几圈。两者的处置不同，
    因此不能共用一个码。
    """

    code = "AGENT-LOOP-001"
