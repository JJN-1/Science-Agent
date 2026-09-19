from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.store.models import Agent

# 阶段 Agent 默认档位（结论类档位用强档，见 §8.2）
STAGE_TIERS: dict[str, str] = {
    "S1": "plan",
    "S2": "synthesize",
    "S3": "plan",
    "S4": "plan",
    "S5": "extract",
    "S6": "synthesize",
    "S7": "write",
    "S8": "synthesize",
}


def get_by_agent_id(session: Session, agent_id: str) -> Agent | None:
    return session.scalar(select(Agent).where(Agent.agent_id == agent_id))


def list_all(session: Session) -> list[Agent]:
    return list(session.scalars(select(Agent).order_by(Agent.agent_id)).all())


def upsert(
    session: Session,
    agent_id: str,
    name: str,
    tier: str,
    role: str = "stage",
    tools: list | None = None,
    budget_steps: int = 20,
    budget_cost: float = 2.0,
) -> Agent:
    agent = get_by_agent_id(session, agent_id)
    if agent is None:
        agent = Agent(
            agent_id=agent_id, name=name, tier=tier, role=role, tools=tools or [],
            budget_steps=budget_steps, budget_cost=budget_cost,
        )
        session.add(agent)
    else:
        agent.name, agent.tier, agent.role = name, tier, role
    return agent
