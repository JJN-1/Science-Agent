from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255))
    domain: Mapped[str] = mapped_column(String(64), default="cs-ai")
    goal: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class BlackboardObject(Base):
    """结构化共享状态：存对象不存聊天记录，同类型版本号自增。"""

    __tablename__ = "blackboard"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    obj_type: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    produced_by: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StageCheckpoint(Base):
    __tablename__ = "stage_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    stage_id: Mapped[str] = mapped_column(String(8), index=True)
    status: Mapped[str] = mapped_column(String(32))
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    stage_id: Mapped[str] = mapped_column(String(8), index=True)
    agent_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="running")
    steps: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Agent(Base):
    """Agent 定义与配置（Sprint 2 契约：档位/工具/预算）。"""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(64), default="stage")
    tier: Mapped[str] = mapped_column(String(32), default="extract")
    tools: Mapped[list] = mapped_column(JSON, default=list)
    budget_steps: Mapped[int] = mapped_column(Integer, default=20)
    budget_cost: Mapped[float] = mapped_column(default=2.0)
    enabled: Mapped[bool] = mapped_column(default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Decision(Base):
    """决策日志：决策与失败尝试都落这里（US-204）。"""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stage_id: Mapped[str] = mapped_column(String(8), index=True)
    agent_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32), default="decision")
    decision: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, default="")
    alternatives: Mapped[list] = mapped_column(JSON, default=list)
    decided_by: Mapped[str] = mapped_column(String(64), default="agent")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LlmUsage(Base):
    """调用记账：project / stage / agent 三维归因（US-203）。"""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stage_id: Mapped[str] = mapped_column(String(8), index=True)
    agent_id: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    tier: Mapped[str] = mapped_column(String(32))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cached: Mapped[bool] = mapped_column(default=False)
    degraded: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Approval(Base):
    """人工审批请求：预算熔断 / 步数熔断 / 危险操作（US-205）。"""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BudgetGrant(Base):
    """预算豁免（FIX-02）：人工批准后追加的额度，使批准真正生效。

    旧实现里批准只是把 approvals.status 改成 approved，预算计数分文未动，
    重跑立刻再次熔断 —— 审批卡住 → 批准 → 又熔断的死循环。豁免是那条缺失的因果链。

    有效限额 = 配置限额 + Σ(未过期豁免.amount)，可按 scope 分别作用于
    项目总额 / 项目每日额度 / Agent 步数 / Agent 成本。
    """

    __tablename__ = "budget_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(String(32), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    amount: Mapped[float] = mapped_column(default=0.0)
    approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProviderSwitchLog(Base):
    """后端切换审计（US-206）。"""

    __tablename__ = "provider_switch_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(32))
    old: Mapped[str | None] = mapped_column(Text, nullable=True)
    new: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="api")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppConfig(Base):
    """运行期状态持久化（§11.4，FIX-05）：键值对，value 为 JSON。

    用途之一是熔断状态（key = ``circuit:<provider>``）。此前熔断只在进程内存里，
    重启即全部清零——被熔断的后端会在重启后立刻重新挨一遍失败。
    """

    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class LlmCache(Base):
    """跨重启的模型响应缓存（FIX-05）。

    此前缓存是 `LlmGateway` 里的进程内 dict：重启即失效，且上限 256 条按插入顺序淘汰。
    落库后带 TTL（默认 7 天），过期即失效并由 `purge_expired` 清理。

    ``cache_key`` 由 (provider, model, tier, messages, schema) 规范化哈希而来，
    因此「哪个后端作答」就是键的一部分——降级作答的结果不会再记到首候选名下。
    """

    __tablename__ = "llm_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    tier: Mapped[str] = mapped_column(String(32))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
