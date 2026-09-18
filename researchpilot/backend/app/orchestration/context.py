from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.store.dao import blackboard as blackboard_dao
from app.store.dao import runs as runs_dao


@dataclass
class StageContext:
    """阶段执行上下文：记录轨迹步骤 + 写黑板，Agent 不直接触碰 DAO 之外的层。"""

    session: Session
    project_id: int
    run_id: int
    agent_id: str

    def think(self, text: str) -> None:
        runs_dao.add_step(self.session, run_id=self.run_id, kind="thought", content={"text": text})

    def record(self, kind: str, content: dict) -> None:
        runs_dao.add_step(self.session, run_id=self.run_id, kind=kind, content=content)

    def write_blackboard(self, obj_type: str, payload: dict, evidence: list | None = None) -> None:
        blackboard_dao.write(
            self.session,
            project_id=self.project_id,
            obj_type=obj_type,
            payload=payload,
            produced_by=self.agent_id,
            evidence=evidence or [],
        )
