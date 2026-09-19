from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.schemas import JobAccepted, PipelineRunRequest, StageOut
from app.orchestration.orchestrator import Orchestrator
from app.store.dao import blackboard as blackboard_dao
from app.store.dao import checkpoints as checkpoint_dao
from app.store.dao import projects as projects_dao

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


@router.post("/api/projects/{project_id}/stages/{stage_id}/run",
             response_model=JobAccepted, status_code=202)
def run_stage(project_id: int, stage_id: str, request: Request,
              session: Session = Depends(get_session)) -> JobAccepted:
    """受理一次阶段运行（FIX-03）：立刻返回 ``job_id``，执行交给作业层。

    受理不阻塞是这条接口的全部意义 —— 分钟级的阶段会撞 HTTP 超时，而且
    用户全程看不到进度。进度从 ``GET /api/jobs/{id}/stream`` 订阅。

    原先那段「失败先 commit 再抛 503」的特判随之消失：受理接口手上已经
    什么都没有可失败的，失败现场的责任搬去了 worker（``JobRunner._settle_failed``）。
    """
    if projects_dao.get(session, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    orchestrator: Orchestrator = request.app.state.orchestrator
    if not orchestrator.registry.has(stage_id):
        raise HTTPException(status_code=404, detail=f"未知阶段: {stage_id}")

    job = request.app.state.job_runner.submit(
        session, project_id=project_id, kind="stage", stage_id=stage_id,
    )
    return JobAccepted(job_id=job.id, status=job.status)


@router.post("/api/projects/{project_id}/pipeline/run",
             response_model=JobAccepted, status_code=202)
def run_pipeline(project_id: int, request: Request,
                 payload: PipelineRunRequest | None = None,
                 session: Session = Depends(get_session)) -> JobAccepted:
    """受理一次全链路运行；可选只跑指定阶段子集。"""
    if projects_dao.get(session, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    orchestrator: Orchestrator = request.app.state.orchestrator

    stage_ids = list(payload.stage_ids) if payload and payload.stage_ids else None
    if stage_ids is not None:
        unknown = [sid for sid in stage_ids if not orchestrator.registry.has(sid)]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知阶段: {', '.join(unknown)}")

    job = request.app.state.job_runner.submit(
        session, project_id=project_id, kind="pipeline",
        params={"stage_ids": stage_ids} if stage_ids else None,
    )
    return JobAccepted(job_id=job.id, status=job.status)


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
