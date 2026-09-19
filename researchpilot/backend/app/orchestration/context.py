from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.ai.client import LlmGateway
from app.store.dao import blackboard as blackboard_dao
from app.store.dao import decisions as decisions_dao
from app.store.dao import runs as runs_dao


@dataclass
class StageContext:
    """阶段执行上下文：轨迹 + 黑板 + 决策日志 + LLM 调用，Agent 不直接触碰 DAO 之外的层。"""

    session: Session
    project_id: int
    run_id: int
    agent_id: str
    stage_id: str = ""
    gateway: LlmGateway | None = None

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

    # ── US-204：决策与失败尝试落 decisions ──────
    def decide(self, decision: str, reason: str = "", alternatives: list | None = None,
               decided_by: str = "agent") -> None:
        decisions_dao.add(
            self.session, project_id=self.project_id, run_id=self.run_id,
            stage_id=self.stage_id, agent_id=self.agent_id, decision=decision,
            reason=reason, alternatives=alternatives, decided_by=decided_by,
            kind="decision",
        )
        self.record("decision", {"text": decision})

    def record_failure(self, failure: str, reason: str = "") -> None:
        decisions_dao.add(
            self.session, project_id=self.project_id, run_id=self.run_id,
            stage_id=self.stage_id, agent_id=self.agent_id, decision=failure,
            reason=reason, kind="failed_attempt",
        )

    # ── US-201/203：模型调用唯一入口 ─────────────
    def llm(self, tier: str, messages: list, schema: dict | None = None,
            max_tokens: int = 1024, temperature: float = 0.7):
        if self.gateway is None:
            raise RuntimeError("gateway 未注入，无法调用模型")
        return self.gateway.call(
            self.session, project_id=self.project_id, run_id=self.run_id,
            stage_id=self.stage_id, agent_id=self.agent_id, tier=tier,
            messages=messages, schema=schema, max_tokens=max_tokens,
            temperature=temperature,
        )
