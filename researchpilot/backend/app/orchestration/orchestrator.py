from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai.budget import BudgetExceeded
from app.ai.client import LlmGateway
from app.orchestration.base import StageAgent
from app.orchestration.context import StageContext
from app.store.dao import approvals as approvals_dao
from app.store.dao import checkpoints as checkpoint_dao
from app.store.dao import decisions as decisions_dao
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

    def __init__(self, registry: StageRegistry, gateway: LlmGateway) -> None:
        self.registry = registry
        self.gateway = gateway

    def run_stage(self, session: Session, project_id: int, stage_id: str) -> int:
        agent = self.registry.get(stage_id)
        run = runs_dao.create_run(
            session, project_id=project_id, stage_id=stage_id, agent_id=agent.agent_id
        )
        ctx = StageContext(
            session=session, project_id=project_id, run_id=run.id,
            agent_id=agent.agent_id, stage_id=stage_id, gateway=self.gateway,
        )
        try:
            writes = agent.run(ctx)
            for w in writes:
                ctx.write_blackboard(w.obj_type, w.payload, w.evidence)
        except BudgetExceeded as exc:
            # US-205：预算熔断 → 暂停 + 审批请求（不算失败）
            runs_dao.finish_run(session, run_id=run.id, status="paused")
            approvals_dao.create(
                session, project_id=project_id, kind="budget", run_id=run.id,
                detail={
                    "source": exc.kind,
                    "stage_id": stage_id,
                    **exc.detail,
                },
            )
            checkpoint_dao.save_checkpoint(
                session, project_id=project_id, stage_id=stage_id, status="paused",
                snapshot={"stage_id": stage_id, "run_id": run.id,
                          "reason": exc.kind},
            )
            return run.id
        except Exception as exc:
            runs_dao.finish_run(session, run_id=run.id, status="failed", error=str(exc))
            decisions_dao.add(
                session, project_id=project_id, run_id=run.id, stage_id=stage_id,
                agent_id=agent.agent_id, decision=f"{stage_id} 运行失败",
                reason=str(exc), kind="failed_attempt",
            )
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
        """顺序调度：按 STAGE_ORDER 执行已注册的阶段。

        任一阶段抛错即向上传播；遇 ``paused``（预算熔断）则**立即中断**，并把
        后续阶段标记为 ``skipped``（FIX-07）。旧实现会带着一个未决审批继续往下跑，
        堆出一串暂停与审批，治理语义完全失效。
        """
        ids = list(stage_ids or STAGE_ORDER)
        run_ids: list[int] = []
        for index, stage_id in enumerate(ids):
            if not self.registry.has(stage_id):
                continue
            run_id = self.run_stage(session, project_id, stage_id)
            run_ids.append(run_id)
            run = runs_dao.get_run(session, run_id)
            if run is not None and run.status == "paused":
                self._skip_remaining(
                    session, project_id, ids[index + 1:],
                    paused_stage_id=stage_id, paused_run_id=run_id,
                )
                break
        return run_ids

    def _skip_remaining(self, session: Session, project_id: int,
                        remaining: list[str], *, paused_stage_id: str,
                        paused_run_id: int) -> None:
        """把暂停点之后的阶段标记为 skipped，并留一条决策日志说明原因。"""
        skipped = [sid for sid in remaining if self.registry.has(sid)]
        if not skipped:
            return
        for stage_id in skipped:
            checkpoint_dao.save_checkpoint(
                session, project_id=project_id, stage_id=stage_id, status="skipped",
                snapshot={
                    "stage_id": stage_id,
                    "reason": "上游阶段暂停（paused），未执行",
                    "paused_stage_id": paused_stage_id,
                    "paused_run_id": paused_run_id,
                },
            )
        decisions_dao.add(
            session, project_id=project_id, run_id=paused_run_id,
            stage_id=paused_stage_id, agent_id=self.registry.get(paused_stage_id).agent_id,
            decision=f"{paused_stage_id} 暂停，跳过后续 {len(skipped)} 个阶段",
            reason="预算熔断需人工审批，避免连环暂停",
            decided_by="system",
        )
