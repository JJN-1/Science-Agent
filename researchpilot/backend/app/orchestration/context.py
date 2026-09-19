from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.ai.client import LlmGateway
from app.jobs.events import LLM_CALL, STEP, emit
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
    job_id: int | None = None

    def think(self, text: str) -> None:
        self.record("thought", {"text": text})

    def record(self, kind: str, content: dict) -> None:
        """轨迹的唯一写入口。``think`` / ``decide`` / Agent 都从这里过。

        因此「实时进度」只需要在这里挂钩子：写完 ``agent_steps`` 后镜像一条
        ``step`` 事件，SSE 就能把 Agent 的每一步送到前端 —— 不必让每个 Agent
        自己记得上报。
        """
        runs_dao.add_step(self.session, run_id=self.run_id, kind=kind, content=content)
        self._emit_step(kind, content)

    def _emit_step(self, kind: str, content: dict) -> None:
        if self.job_id is None:
            return
        emit(self.session, self.job_id, STEP,
             {"kind": kind, "content": content, "run_id": self.run_id})

    def _emit_llm_step(self, response, cost: float, cached: bool) -> None:  # noqa: ANN001
        """模型调用的结算事件：让「正在等模型」这件事在流里可见。"""
        if self.job_id is None:
            return
        emit(self.session, self.job_id, LLM_CALL, {
            "provider": response.provider,
            "model": response.model,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "cost": round(cost, 6),
            "latency_ms": response.latency_ms,
            "cached": cached,
            "degraded": list(response.degraded),
            "run_id": self.run_id,
        })

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
            temperature=temperature, on_step=self._emit_llm_step,
        )
