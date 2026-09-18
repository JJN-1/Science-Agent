from __future__ import annotations

from sqlalchemy.orm import Session

from app.orchestration.base import StageAgent
from app.orchestration.context import StageContext
from app.store.dao import checkpoints as checkpoint_dao
from app.store.dao import runs as runs_dao

STAGE_ORDER = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]


class StageRegistry:
    def __init__(self) -> None:
        self._stages: dict[str, StageAgent] = {}

    def register(self, agent: StageAgent) -> None:
        if agent.stage_id in self._stages:
            raise ValueError(f"stage {agent.stage_id} already registered")
        self._stages[agent.stage_id] = agent

    def get(self, stage_id: str) -> StageAgent:
        if stage_id not in self._stages:
            raise KeyError(f"unknown stage: {stage_id}")
        return self._stages[stage_id]

    def has(self, stage_id: str) -> bool:
        return stage_id in self._stages

    def all(self) -> list[StageAgent]:
        return sorted(self._stages.values(), key=lambda a: STAGE_ORDER.index(a.stage_id))

    def stage_ids(self) -> list[str]:
        return [agent.stage_id for agent in self.all()]


class Orchestrator:
    """Supervisor 骨架：单阶段调度 + 顺序 pipeline + 检查点。回退边在 Sprint 4 加入。"""

    def __init__(self, registry: StageRegistry) -> None:
        self.registry = registry

    def run_stage(self, session: Session, project_id: int, stage_id: str) -> int:
        agent = self.registry.get(stage_id)
        run = runs_dao.create_run(
            session, project_id=project_id, stage_id=stage_id, agent_id=agent.agent_id
        )
        ctx = StageContext(
            session=session, project_id=project_id, run_id=run.id, agent_id=agent.agent_id
        )
        try:
            writes = agent.run(ctx)
            for w in writes:
                ctx.write_blackboard(w.obj_type, w.payload, w.evidence)
        except Exception as exc:
            runs_dao.finish_run(session, run_id=run.id, status="failed", error=str(exc))
            checkpoint_dao.save_checkpoint(
                session,
                project_id=project_id,
                stage_id=stage_id,
                status="failed",
                snapshot={"stage_id": stage_id, "run_id": run.id, "error": str(exc)},
            )
            raise
        runs_dao.finish_run(session, run_id=run.id, status="succeeded")
        checkpoint_dao.save_checkpoint(
            session,
            project_id=project_id,
            stage_id=stage_id,
            status="succeeded",
            snapshot={
                "stage_id": stage_id,
                "agent_id": agent.agent_id,
                "run_id": run.id,
                "writes": [
                    {"obj_type": w.obj_type, "payload": w.payload} for w in writes
                ],
            },
        )
        return run.id

    def run_pipeline(self, session: Session, project_id: int,
                     stage_ids: list[str] | None = None) -> list[int]:
        """顺序调度：按 STAGE_ORDER 执行已注册的阶段，任一失败即中断。"""
        ids = stage_ids or STAGE_ORDER
        run_ids: list[int] = []
        for stage_id in ids:
            if self.registry.has(stage_id):
                run_ids.append(self.run_stage(session, project_id, stage_id))
        return run_ids
