from __future__ import annotations

from sqlalchemy.orm import Session

from app.store.dao import agents as agents_dao
from app.store.dao import grants as grants_dao
from app.store.dao import usage as usage_dao
from app.store.models import AgentRun


class BudgetExceeded(Exception):
    """预算/步数熔断（US-205）：由 Orchestrator 捕获转为暂停 + 审批。"""

    code = "LLM-BUDGET-001"

    def __init__(self, kind: str, detail: dict) -> None:
        super().__init__(f"budget exceeded: {kind}")
        self.kind = kind  # project_total | project_daily | agent_steps | agent_cost
        self.detail = detail


# 批准后默认追加的额度（可被 config 的 ai.budget.approval_grant 覆盖）。
DEFAULT_GRANT_POLICY: dict[str, float] = {
    "project_total": 50.0,
    "project_daily": 10.0,
    "agent_steps": 20.0,
    "agent_cost": 2.0,
}


class BudgetManager:
    """项目级预算 + Agent 级步数/成本上限（§8.5）。

    **有效限额 = 配置限额 + Σ(未过期豁免)**（FIX-02）。旧实现只比对配置限额，
    批准审批不改变任何计数，于是「暂停 → 批准 → 立刻再暂停」成为死循环，
    且每次批准都堆一条新的 pending 审批。
    """

    def __init__(self, budget_cfg: dict) -> None:
        self.project_total = float(budget_cfg.get("project_total", 50.0))
        self.project_daily = float(budget_cfg.get("project_daily", 10.0))
        raw_policy = budget_cfg.get("approval_grant") or {}
        self.grant_policy: dict[str, float] = {
            **DEFAULT_GRANT_POLICY,
            **{str(k): float(v) for k, v in raw_policy.items()},
        }

    def suggested_grant(self, kind: str) -> float:
        """该熔断类型批准后的建议追加额度，随审批单一起展示给用户。"""
        return float(self.grant_policy.get(kind, 0.0))

    def check(self, session: Session, project_id: int, agent_id: str, run_id: int) -> None:
        total_limit = self.project_total + grants_dao.granted(
            session, project_id, "project_total"
        )
        spend = usage_dao.project_spend(session, project_id)
        if spend >= total_limit:
            raise BudgetExceeded("project_total", {
                "message": f"项目累计成本 ¥{spend:.2f} 已达总额上限 ¥{total_limit:.2f}",
                "spend": spend, "limit": total_limit,
            })

        daily_limit = self.project_daily + grants_dao.granted(
            session, project_id, "project_daily"
        )
        daily = usage_dao.project_spend_today(session, project_id)
        if daily >= daily_limit:
            raise BudgetExceeded("project_daily", {
                "message": f"项目今日成本 ¥{daily:.2f} 已达每日额度 ¥{daily_limit:.2f}",
                "spend": daily, "limit": daily_limit,
            })

        agent = agents_dao.get_by_agent_id(session, agent_id)
        if agent is None:
            return
        run = session.get(AgentRun, run_id)
        if run is None:
            return

        steps_limit = agent.budget_steps + grants_dao.granted(
            session, project_id, "agent_steps", agent_id=agent_id
        )
        if run.steps >= steps_limit:
            raise BudgetExceeded("agent_steps", {
                "message": f"{agent_id} 步数 {run.steps} 已达上限 {steps_limit:.0f}",
                "steps": run.steps, "limit": steps_limit,
            })

        cost_limit = agent.budget_cost + grants_dao.granted(
            session, project_id, "agent_cost", agent_id=agent_id
        )
        run_cost = usage_dao.run_cost(session, run_id)
        if run_cost >= cost_limit:
            raise BudgetExceeded("agent_cost", {
                "message": f"{agent_id} 本次运行成本 ¥{run_cost:.2f} 已达上限 ¥{cost_limit:.2f}",
                "cost": run_cost, "limit": cost_limit,
            })
