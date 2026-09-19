from __future__ import annotations

import keyring

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.ai.providers.openai_compat import KEYRING_SERVICE
from app.ai.routing import RouteCandidate, RoutingError
from app.api.deps import get_session
from app.config import load_config
from app.store.models import ProviderSwitchLog

router = APIRouter(prefix="/api/settings", tags=["settings"])


class TierRoute(BaseModel):
    provider: str
    model: str


class RoutingPatch(BaseModel):
    tier: str
    candidates: list[TierRoute]


class ProviderKey(BaseModel):
    key: str


@router.get("/providers")
def providers(request: Request) -> list[dict]:
    registry = request.app.state.ai_registry
    return registry.health_report()


@router.post("/providers/reload")
def reload_providers(request: Request,
                     session: Session = Depends(get_session)) -> dict:
    """全局级热切换（§8.4）：按最新配置重建注册表，写切换审计。"""
    config = load_config()
    registry = request.app.state.ai_registry
    before = registry.snapshot()
    delta = registry.reload(config)
    session.add(ProviderSwitchLog(
        scope="global", old=",".join(before), new=",".join(registry.snapshot()),
        source="api_reload",
    ))
    session.commit()
    return {"providers": registry.names(), **delta}


@router.patch("/routing")
def patch_routing(patch: RoutingPatch, request: Request,
                  session: Session = Depends(get_session)) -> dict:
    """档位级热切换（§8.4）。"""
    router_obj = request.app.state.router
    ai_registry = request.app.state.ai_registry
    old = router_obj.as_config().get(patch.tier)
    try:
        router_obj.update_tier(
            patch.tier,
            [RouteCandidate(provider=c.provider, model=c.model) for c in patch.candidates],
            ai_registry.providers_map(),
        )
    except RoutingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    new = [
        {"provider": c.provider, "model": c.model}
        for c in router_obj.candidates(patch.tier)
    ]
    session.add(ProviderSwitchLog(
        scope=f"tier:{patch.tier}", old=str(old), new=str(new), source="api_routing",
    ))
    session.commit()
    return {"tier": patch.tier, "candidates": new}


@router.get("/routing")
def get_routing(request: Request) -> dict:
    return request.app.state.router.as_config()


@router.put("/providers/{name}/key")
def set_provider_key(name: str, body: ProviderKey, request: Request) -> dict:
    """API Key 写入 Windows 凭据管理器（§8.4），config 不落明文。"""
    try:
        keyring.set_password(KEYRING_SERVICE, name, body.key)
    except Exception as exc:  # keyring 后端不可用（如无桌面环境）
        raise HTTPException(status_code=500, detail=f"凭据写入失败: {exc}") from None
    # 让下一次 health() 立即重新探测，而不是等 TTL 过期（FIX-04）
    registry = getattr(request.app.state, "ai_registry", None)
    if registry is not None and name in registry.names():
        provider = registry.get(name)
        invalidate = getattr(provider, "invalidate_health", None)
        if callable(invalidate):
            invalidate()
    return {"provider": name, "stored": True}
