from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.demo_stage import register_all
from app.api import projects, runs, stages
from app.config import ensure_data_dir, load_config
from app.observability.logging import get_logger, setup_logging
from app.orchestration.orchestrator import Orchestrator, StageRegistry
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
    app.state.session_factory = make_session_factory(engine)
    registry = StageRegistry()
    register_all(registry)
    app.state.orchestrator = Orchestrator(registry)
    logger.info("startup_complete", data_dir=str(root))
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

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
