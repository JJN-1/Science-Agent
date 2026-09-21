from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent_kernel import specs as kernel_specs
from app.agent_kernel.tools.pipeline import RunPipelineTool
from app.agent_kernel.tools.registry import ToolRegistry
from app.agents.demo_stage import register_all
from app.ai.budget import BudgetManager
from app.ai.client import LlmGateway
from app.ai.registry import ProviderRegistry as AIProviderRegistry
from app.ai.routing import Router
from app.api import (
    conversations,
    governance,
    jobs,
    projects,
    runs,
    settings,
    stages,
    task_plans,
)
from app.api import tools as tools_api
from app.config import ensure_data_dir, load_config
from app.jobs.runner import JobRunner
from app.observability.logging import get_logger, setup_logging
from app.orchestration.orchestrator import STAGE_ORDER, Orchestrator, StageRegistry
from app.store.dao import agents as agents_dao
from app.store.dao import jobs as jobs_dao
from app.store.dao import llm_cache as llm_cache_dao
from app.store.db import database_path, make_engine, make_session_factory
from app.store.migrations import upgrade_to_head

logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    setup_logging(config.get("log", {}).get("level", "INFO"))
    root = ensure_data_dir()  # US-102：首启自动创建数据目录与配置
    engine = make_engine(database_path(root))
    upgrade_to_head(engine)  # 幂等迁移，保证零配置首启即可用
    app.state.engine = engine
    session_factory = make_session_factory(engine)
    app.state.session_factory = session_factory
    app.state.config = config

    # AI 接入层（US-201/206）：健康过滤 + 档位路由 + 预算
    ai_cfg = config.get("ai", {}) or {}
    ai_registry = AIProviderRegistry.from_config(config)
    router = Router.from_config(config, ai_registry.providers_map())
    gateway = LlmGateway(
        ai_registry, router,
        BudgetManager(ai_cfg.get("budget", {})),
        ai_cfg.get("cache", {}),
    )
    app.state.ai_registry = ai_registry
    app.state.router = router
    app.state.gateway = gateway

    registry = StageRegistry()
    register_all(registry)
    app.state.orchestrator = Orchestrator(registry, gateway)

    # 内核工具注册表（US-404）。`run_pipeline` 把 S1–S8 确定性编排作为**一个能力**
    # 暴露出来（§4.1「S1–S8 确定性编排作为其一种技能」），但调度状态机仍在 Orchestrator
    # 手里（D1）。编排函数是**注入**的：内核层不 import 编排层（方向是编排 → 内核）。
    tool_registry = ToolRegistry()
    tool_registry.register(RunPipelineTool(
        runner=app.state.orchestrator.run_pipeline,
        stage_ids=STAGE_ORDER,
    ))
    app.state.tool_registry = tool_registry
    # 白名单里写了、注册表里没有的工具：**加载期只告警**（与 Router.from_config 同一条
    # 约定 —— 阶段二某个工具还没实现，不该让整个应用起不来）。
    missing_tools = kernel_specs.unknown_tools(set(tool_registry.names()))
    if missing_tools:
        logger.warning("agent_spec_unknown_tools", missing=missing_tools)

    # 异步作业层（FIX-03）：受理即返回，执行交给进程内单 worker。
    # orchestrator 用 provider 延迟取，热重载后仍拿到最新的那一个。
    job_runner = JobRunner(session_factory, lambda: app.state.orchestrator)
    app.state.job_runner = job_runner

    # agents 表播种（US-201 契约：档位/预算随 Agent 定义）+ 运行期状态恢复（FIX-05）
    with session_factory() as session:
        for agent in registry.all():
            # US-404：档位/预算/工具白名单一律以 `AgentSpec` 为唯一来源（§5.3）。
            # 不传 spec 的 Agent 保持既有值不动（upsert 的 `None` = 不覆盖）。
            spec = kernel_specs.by_agent_id(agent.agent_id)
            agents_dao.upsert(
                session, agent_id=agent.agent_id, name=agent.name,
                tier=agents_dao.STAGE_TIERS.get(agent.stage_id, "extract"),
                tools=list(spec.tools) if spec else None,
                budget_steps=spec.max_steps if spec else None,
                budget_cost=spec.max_cost_usd if spec else None,
            )
        ai_registry.load_circuits(session)      # 熔断状态跨重启保留
        llm_cache_dao.purge_expired(session)    # 清掉过期缓存
        recovered = jobs_dao.recover_orphans(session)  # 僵尸作业自愈
        session.commit()
    if recovered:
        logger.warning("jobs_recovered_from_crash", count=recovered)

    job_runner.start()
    logger.info("startup_complete", data_dir=str(root),
                providers=ai_registry.names())
    yield
    await job_runner.stop()
    engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="ResearchPilot", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(projects.router)
    app.include_router(conversations.router)
    app.include_router(task_plans.router)
    app.include_router(stages.router)
    app.include_router(jobs.router)
    app.include_router(runs.router)
    app.include_router(governance.router)
    app.include_router(settings.router)
    app.include_router(tools_api.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
