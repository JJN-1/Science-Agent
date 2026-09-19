from __future__ import annotations

from sqlalchemy.orm import Session

from app.store.dao import agents as agents_dao
from app.store.dao import usage as usage_dao
from app.store.models import AgentRun


class BudgetExceeded(Exception):
    """预算/步数熔断（US-205）：由 Orchestrator 捕获转为暂停 + 审批。"""

    code = "LLM-BUDGET-001"

    def __init__(self, kind: str, detail: dict) -> None:
        super().__init__(f"budget exceeded: {kind}")
        self.kind = kind  # project_total | project_daily | agent_steps | agent_cost
        self.detail = detail


class BudgetManager:
    """项目级预算 + Agent 级步数/成本上限（§8.5）。"""

    def __init__(self, budget_cfg: dict) -> None:
        self.project_total = float(budget_cfg.get("project_total", 50.0))
        self.project_daily = float(budget_cfg.get("project_daily", 10.0))

    def check(self, session: Session, project_id: int, agent_id: str, run_id: int) -> None:
        spend = usage_dao.project_spend(session, project_id)
        if spend >= self.project_total:
            raise BudgetExceeded("project_total", {
                "message": f"项目累计成本 ¥{spend:.2f} 已达总额上限 ¥{self.project_total:.2f}",
                "spend": spend, "limit": self.project_total,
            })
        daily = usage_dao.project_spend_today(session, project_id)
        if daily >= self.project_daily:
            raise BudgetExceeded("project_daily", {
                "message": f"项目今日成本 ¥{daily:.2f} 已达每日额度 ¥{self.project_daily:.2f}",
                "spend": daily, "limit": self.project_daily,
            })
        agent = agents_dao.get_by_agent_id(session, agent_id)
        if agent is None:
            return
        run = session.get(AgentRun, run_id)
        if run is None:
            return
        if run.steps >= agent.budget_steps:
            raise BudgetExceeded("agent_steps", {
                "message": f"{agent_id} 步数 {run.steps} 已达上限 {agent.budget_steps}",
                "steps": run.steps, "limit": agent.budget_steps,
            })
        run_cost = usage_dao.run_cost(session, run_id)
        if run_cost >= agent.budget_cost:
            raise BudgetExceeded("agent_cost", {
                "message": f"{agent_id} 本次运行成本 ¥{run_cost:.2f} 已达上限 ¥{agent.budget_cost:.2f}",
                "cost": run_cost, "limit": agent.budget_cost,
            })
