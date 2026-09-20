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
