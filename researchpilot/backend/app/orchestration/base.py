from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class BlackboardWrite:
    """阶段产物：写入黑板的结构化对象。"""

    obj_type: str
    payload: dict
    evidence: list = field(default_factory=list)


class StageAgent(ABC):
    """阶段 Agent 基类：Sprint 2 起补齐 AgentSpec 契约（档位/工具/预算）。"""

    stage_id: str
    agent_id: str
    name: str
    description: str = ""

    @abstractmethod
    def run(self, ctx) -> list[BlackboardWrite]:  # noqa: ANN001
        """执行阶段逻辑，返回要写入黑板的对象列表。"""
