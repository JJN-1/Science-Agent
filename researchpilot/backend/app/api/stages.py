from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.ai.base import ProviderError
from app.api.deps import get_session
from app.api.schemas import RunStageResponse, StageOut
from app.orchestration.orchestrator import Orchestrator
from app.store.dao import blackboard as blackboard_dao
from app.store.dao import checkpoints as checkpoint_dao
from app.store.dao import projects as projects_dao
from app.store.dao import runs as runs_dao

router = APIRouter(tags=["stages"])


@router.get("/api/stages", response_model=list[StageOut])
def list_stages(request: Request) -> list[StageOut]:
    orchestrator: Orchestrator = request.app.state.orchestrator
    return [
        StageOut(
            stage_id=agent.stage_id,
            agent_id=agent.agent_id,
            name=agent.name,
            description=agent.description,
            implemented=agent.implemented,
            planned_sprint=agent.planned_sprint,
        )
        for agent in orchestrator.registry.all()
    ]


@router.post("/api/projects/{project_id}/stages/{stage_id}/run", response_model=RunStageResponse)
def run_stage(project_id: int, stage_id: str, request: Request,
              session: Session = Depends(get_session)) -> RunStageResponse:
    if projects_dao.get(session, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    orchestrator: Orchestrator = request.app.state.orchestrator
    try:
        run_id = orchestrator.run_stage(session, project_id, stage_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知阶段: {stage_id}") from None
    except Exception as exc:
        # Orchestrator 已经把失败状态（run=failed / checkpoint / failed_attempt / 原始输出）
        # 写进了 session，但 get_session 在异常时统一 rollback，会把这些记录一并丢掉——
        # 前端「详见流内记录」于是指向空，最该留的失败现场反而没了。
        # 这里先提交失败记录，再返回结构化的可操作错误。
        try:
            session.commit()
        except Exception:
            session.rollback()
        if isinstance(exc, ProviderError):
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": str(exc)},
            ) from None
        raise HTTPException(status_code=500, detail=f"阶段执行失败: {exc}") from None
    run = runs_dao.get_run(session, run_id)
    return RunStageResponse(run_id=run_id, status=run.status if run else "unknown")


@router.get("/api/projects/{project_id}/blackboard")
def project_blackboard(project_id: int, session: Session = Depends(get_session)) -> list[dict]:
    if projects_dao.get(session, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return [
        {
            "id": obj.id,
            "obj_type": obj.obj_type,
            "version": obj.version,
            "payload": obj.payload,
            "produced_by": obj.produced_by,
            "evidence": obj.evidence,
            "created_at": obj.created_at.isoformat(),
        }
        for obj in blackboard_dao.list_for_project(session, project_id)
    ]


@router.get("/api/projects/{project_id}/checkpoints")
def project_checkpoints(project_id: int, session: Session = Depends(get_session)) -> list[dict]:
    if projects_dao.get(session, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return [
        {
            "id": cp.id,
            "stage_id": cp.stage_id,
            "status": cp.status,
            "snapshot": cp.snapshot,
            "created_at": cp.created_at.isoformat(),
        }
        for cp in checkpoint_dao.list_for_project(session, project_id)
    ]
