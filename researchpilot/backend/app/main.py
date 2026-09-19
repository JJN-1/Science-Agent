from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.demo_stage import register_all
from app.ai.budget import BudgetManager
from app.ai.client import LlmGateway
from app.ai.registry import ProviderRegistry as AIProviderRegistry
from app.ai.routing import Router
from app.api import governance, projects, runs, settings, stages
from app.config import ensure_data_dir, load_config
from app.observability.logging import get_logger, setup_logging
from app.orchestration.orchestrator import Orchestrator, StageRegistry
from app.store.dao import agents as agents_dao
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

    # agents 表播种（US-201 契约：档位/预算随 Agent 定义）+ 运行期状态恢复（FIX-05）
    with session_factory() as session:
        for agent in registry.all():
            agents_dao.upsert(
                session, agent_id=agent.agent_id, name=agent.name,
                tier=agents_dao.STAGE_TIERS.get(agent.stage_id, "extract"),
            )
        ai_registry.load_circuits(session)      # 熔断状态跨重启保留
        llm_cache_dao.purge_expired(session)    # 清掉过期缓存
        session.commit()

    logger.info("startup_complete", data_dir=str(root),
                providers=ai_registry.names())
    yield
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
    app.include_router(stages.router)
    app.include_router(runs.router)
    app.include_router(governance.router)
    app.include_router(settings.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
