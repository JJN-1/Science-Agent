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
    budget_steps: int | None = None,
    budget_cost: float | None = None,
) -> Agent:
    """插入或更新一个 Agent 行。

    ``tools`` / ``budget_steps`` / ``budget_cost`` 与 ``name`` / ``tier`` 的区别是
    **``None`` 表示「本次不动这个字段」**，而不是「用默认值覆盖」：

    - ``agents.tools`` 在 US-404 之前是一列死数据（没有任何读取点），一旦开始由
      ``AgentSpec`` 播种，就必须**连已存在的行也更新** —— 只更新插入路径的话，
      升级上来的安装永远拿不到白名单，表现为「新装的能用、老装的永远被拒」
    - ``budget_steps=0`` 是合法值（测试用它模拟「一步就熔断」），所以判据必须是
      ``is not None`` 而不是真值判断
    """
    agent = get_by_agent_id(session, agent_id)
    if agent is None:
        agent = Agent(
            agent_id=agent_id, name=name, tier=tier, role=role,
            tools=list(tools or []),
            budget_steps=20 if budget_steps is None else budget_steps,
            budget_cost=2.0 if budget_cost is None else budget_cost,
        )
        session.add(agent)
        return agent

    agent.name, agent.tier, agent.role = name, tier, role
    if tools is not None:
        agent.tools = list(tools)
    if budget_steps is not None:
        agent.budget_steps = budget_steps
    if budget_cost is not None:
        agent.budget_cost = budget_cost
    return agent
