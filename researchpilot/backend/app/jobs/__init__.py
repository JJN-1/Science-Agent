"""异步任务层（FIX-03 / ADR-0003）。

受理与执行解耦：``JobRunner`` 负责受理与调度，``events`` 负责把执行过程写成
可被 SSE 增量投递的事件日志。事件以数据库为准，不做进程内扇出。
"""

from app.jobs.events import emit
from app.jobs.runner import JobRunner

__all__ = ["JobRunner", "emit"]
