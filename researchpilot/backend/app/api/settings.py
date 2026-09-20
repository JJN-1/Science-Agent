from __future__ import annotations

from collections import defaultdict

import keyring
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.ai.base import ProviderUnavailable
from app.ai.provider_config import (
    KNOWN_CAPABILITIES,
    ProviderConfigError,
    normalize,
    normalize_base_url,
)
from app.ai.providers.openai_compat import KEYRING_SERVICE, OpenAICompatProvider
from app.ai.registry import PROVIDER_TYPES, ProviderRegistry
from app.ai.routing import RouteCandidate, Router, RoutingError
from app.api.deps import get_session
from app.config import (
    default_provider_names,
    load_config,
    read_user_config,
    remove_user_provider,
    upsert_user_provider,
    upsert_user_routing,
)
from app.store.dao import agents as agents_dao
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


class ProviderIn(BaseModel):
    """接入表单（US-312）：字段与设计 §8.1 的 provider 配置一致。"""

    name: str
    type: str
    base_url: str | None = None
    models: list[str] | None = None
    model: str | None = None            # 兼容旧的单数写法
    vendor: str | None = None
    api_key_ref: str | None = None      # 显式留空 = 该端点无需鉴权
    capabilities: list[str] | None = None
    price: dict | None = None
    price_per_1k: dict | None = None    # 兼容旧字段名
    timeout_s: float | None = None
    extra_headers: dict | None = None
    extra_body: dict | None = None


class ProviderPatch(BaseModel):
    type: str | None = None
    base_url: str | None = None
    models: list[str] | None = None
    model: str | None = None
    vendor: str | None = None
    api_key_ref: str | None = None
    capabilities: list[str] | None = None
    price: dict | None = None
    price_per_1k: dict | None = None
    timeout_s: float | None = None
    extra_headers: dict | None = None
    extra_body: dict | None = None


class ProbeIn(BaseModel):
    """探测模型时允许带上尚未保存的草稿，省掉「先保存再试探」的来回。"""

    base_url: str | None = None
    api_key: str | None = None
    api_key_ref: str | None = None
    # 显式「该端点无需鉴权」（本地自建端点）。与 api_key_ref="" 等价，
    # 但给表单一个明确的勾选框 —— 用户勾了无需鉴权却仍被要求填 Key，是实打实的投诉。
    keyless: bool | None = None


# ── 内部工具 ────────────────────────────────────

def _effective_providers() -> dict:
    return (load_config().get("ai", {}) or {}).get("providers", {}) or {}


def _registry(request: Request):
    registry = getattr(request.app.state, "ai_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="AI 注册表尚未就绪")
    return registry


def _apply_config(request: Request, session: Session, *, scope: str,
                  old: str, new: str, source: str) -> dict:
    """把最新配置推到运行期：重建注册表 + 原地替换路由 + 写审计。

    provider 的新增/修改/删除都必须走这里 —— 用户的要求是「填完即可用、无需重启」。
    """
    config = load_config()
    registry = _registry(request)
    delta = registry.reload(config, session)
    # 路由原地替换：LlmGateway 持有 Router 引用，换实例不会生效
    request.app.state.router.replace(config, registry.providers_map())
    session.add(ProviderSwitchLog(scope=scope, old=old, new=new, source=source))
    return delta


def _dry_run(config: dict) -> None:
    """落盘前先试装一次：装不上就不写配置，用户拿到的是可读的报错而不是半残状态。

    不带健康探测——校验配置结构用不着真去打网络。
    """
    trial = ProviderRegistry.from_config(config, probe_health=False)
    Router.from_config(config, trial.providers_map())


def _with_provider(name: str, provider_cfg: dict | None) -> dict:
    """在内存里算出「写入后」的完整配置（不落盘），供试装使用。"""
    effective = load_config()
    providers = effective.setdefault("ai", {}).setdefault("providers", {})
    if provider_cfg is None:
        providers.pop(name, None)
    else:
        providers[name] = provider_cfg
    return effective


def _invalidate(request: Request, name: str) -> None:
    """失效该 provider 的健康探测缓存，使下一次 health() 立即重新探测。"""
    registry = getattr(request.app.state, "ai_registry", None)
    if registry is None or name not in registry.names():
        return
    invalidate = getattr(registry.get(name), "invalidate_health", None)
    if callable(invalidate):
        invalidate()


def _references(request: Request, session: Session) -> dict[str, list[str]]:
    """统计每个 provider 的引用位置：档位路由 + Agent 配置（不级联删除）。

    路由取**运行期**的那份：`PATCH /routing` 是内存热切换，只读配置文件会漏判。
    """
    refs: dict[str, list[str]] = defaultdict(list)
    router_obj = getattr(request.app.state, "router", None)
    routes = router_obj.as_config() if router_obj is not None else (
        (load_config().get("ai", {}) or {}).get("routing", {}) or {}
    )
    for tier, chain in routes.items():
        for cand in chain or []:
            refs[str(cand.get("provider"))].append(f"routing:{tier}")
    for agent in agents_dao.list_all(session):
        provider = (agent.config or {}).get("provider")
        if provider:
            refs[str(provider)].append(f"agent:{agent.agent_id}")
    return {name: sorted(set(where)) for name, where in refs.items()}


def _build_probe_provider(name: str, body: ProbeIn) -> OpenAICompatProvider:
    """给探测模型用的一次性 provider（不落配置、不进注册表）。"""
    raw = dict(_effective_providers().get(name) or {})
    if body.base_url is not None:
        raw["base_url"] = body.base_url
    # 草稿里的凭据语义要和保存路径**完全一致**，否则会出现「保存后能用、探测时说缺 Key」
    if body.keyless:
        raw["api_key_ref"] = ""
    elif body.api_key_ref is not None:
        raw["api_key_ref"] = body.api_key_ref
    if raw.get("type") == "mock":
        raise HTTPException(status_code=400, detail="mock 类型没有远端模型清单可探测")
    if not raw.get("base_url"):
        raise HTTPException(status_code=400, detail="请先填写 base_url 再探测模型")
    try:
        base_url = normalize_base_url(raw["base_url"])
    except ProviderConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # None = 用户没提过这件事 → 缺省引用名即 provider 名；"" = 显式无需鉴权。
    # 旧实现写 `or name`，把「显式留空」当成了「没填」—— 勾了无需鉴权照样要 Key。
    ref = raw.get("api_key_ref")
    provider = OpenAICompatProvider(name, {
        "base_url": base_url,
        "models": [raw.get("model") or "probe"],
        "vendor": raw.get("vendor") or "probe",
        "api_key_ref": name if ref is None else str(ref).strip(),
        "extra_headers": raw.get("extra_headers") or {},
    })
    if body.api_key and provider.auth_required:
        # 草稿里的 Key 只用于本次探测，不落凭据管理器
        provider._peek_key = lambda: body.api_key  # type: ignore[method-assign]
    return provider


# ── Provider 列表 / 增删改 ───────────────────────

@router.get("/providers")
def providers(request: Request, session: Session = Depends(get_session)) -> list[dict]:
    registry = _registry(request)
    refs = _references(request, session)
    user_names = set((read_user_config().get("ai", {}) or {}).get("providers", {}) or {})
    builtin = default_provider_names()
    return [
        {
            **row,
            "referenced_by": refs.get(row["name"], []),
            "source": "user" if row["name"] in user_names else (
                "builtin" if row["name"] in builtin else "runtime"
            ),
            "deletable": row["name"] in user_names and row["name"] not in builtin,
        }
        for row in registry.health_report()
    ]


@router.post("/providers")
def create_provider(body: ProviderIn, request: Request,
                    session: Session = Depends(get_session)) -> dict:
    """新增自定义 provider（US-312）。"""
    if body.name in _effective_providers():
        raise HTTPException(status_code=409, detail=f"provider 已存在: {body.name}")
    raw = body.model_dump(exclude_none=True)
    name = raw.pop("name")
    try:
        normalized = normalize(name, raw)
    except ProviderConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    try:
        _dry_run(_with_provider(name, normalized))
    except (ValueError, RoutingError) as exc:
        raise HTTPException(status_code=400, detail=f"配置无法生效: {exc}") from None

    upsert_user_provider(name, normalized)
    old = ",".join(_registry(request).names())
    _apply_config(request, session, scope=f"provider:{name}", old=old,
                  new=",".join(_registry(request).names()),
                  source="api_provider_create")
    _invalidate(request, name)
    session.commit()
    return {"provider": name, "config": normalized}


@router.patch("/providers/{name}")
def update_provider(name: str, body: ProviderPatch, request: Request,
                    session: Session = Depends(get_session)) -> dict:
    """修改 provider（US-312）；只提交需要改的字段，其余沿用现值。"""
    current = _effective_providers().get(name)
    if current is None:
        raise HTTPException(status_code=404, detail=f"provider 不存在: {name}")
    if name in default_provider_names():
        raise HTTPException(
            status_code=400,
            detail=f"{name} 是内置 provider（config/default.yaml），请勿在此修改；"
                   f"可在用户 config.yaml 中覆盖",
        )
    merged = {**current, **body.model_dump(exclude_none=True)}
    merged.pop("name", None)
    try:
        normalized = normalize(name, merged)
    except ProviderConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    try:
        _dry_run(_with_provider(name, normalized))
    except (ValueError, RoutingError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"配置无法生效，已保持原样: {exc}",
        ) from None

    upsert_user_provider(name, normalized)
    _apply_config(request, session, scope=f"provider:{name}",
                  old=str(current), new=str(normalized),
                  source="api_provider_update")
    _invalidate(request, name)               # 改了 base_url / capabilities 必须重探
    session.commit()
    return {"provider": name, "config": normalized}


@router.delete("/providers/{name}")
def delete_provider(name: str, request: Request,
                    session: Session = Depends(get_session)) -> dict:
    """删除 provider；被档位路由或 Agent 引用时拒绝并列出引用位置，不做级联删除。"""
    if name not in _effective_providers():
        raise HTTPException(status_code=404, detail=f"provider 不存在: {name}")
    if name in default_provider_names():
        raise HTTPException(
            status_code=400,
            detail=f"{name} 是内置 provider（config/default.yaml），不能删除",
        )
    referenced = _references(request, session).get(name) or []
    if referenced:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"provider {name} 仍被引用，请先解除引用再删除",
                "referenced_by": referenced,
            },
        )

    removed = remove_user_provider(name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"provider 不存在于用户配置: {name}")
    _apply_config(request, session, scope=f"provider:{name}", old=name, new="",
                  source="api_provider_delete")
    session.commit()
    return {"provider": name, "removed": True}


@router.post("/providers/{name}/probe-models")
def probe_models(name: str, request: Request, body: ProbeIn | None = None) -> dict:
    """探测端点可用模型清单（US-312）。

    只取 `id`：上游清单不含能力与价格，这两项一律以本地配置为准（设计 §8.1）。
    端点未实现 `GET {base_url}/models` 时返回 ok=false + 提示，让用户手工填写模型 ID。
    允许带上尚未保存的 `base_url`，省掉「先保存再试探」的来回。
    """
    draft = body or ProbeIn()
    registry = getattr(request.app.state, "ai_registry", None)
    known = (registry is not None and name in registry.names()) \
        or name in _effective_providers()
    if not known and not draft.base_url:
        raise HTTPException(
            status_code=404,
            detail=f"provider 不存在: {name}（新端点请先填写 base_url）",
        )

    target = _build_probe_provider(name, draft)
    try:
        models = target.list_remote_models()
    except ProviderUnavailable as exc:
        # 缺 Key / 端点不可达这类「补一下就能好」的原因，直接给动作指引
        return {"provider": name, "ok": False, "models": [],
                "detail": f"{exc}。录入 Key 或勾选「该端点无需鉴权」后重试。"}
    except Exception as exc:  # 协议不兼容等，只能请用户手工填
        return {"provider": name, "ok": False, "models": [],
                "detail": f"无法从该端点获取模型清单（{exc}）。请手工填写模型 ID。"}
    if not models:
        return {"provider": name, "ok": False, "models": [],
                "detail": "端点返回的模型清单为空，请手工填写模型 ID。"}
    return {"provider": name, "ok": True, "models": models, "detail": None}


# ── 档位路由 / 热重载 / Key ──────────────────────

@router.post("/providers/reload")
def reload_providers(request: Request,
                     session: Session = Depends(get_session)) -> dict:
    """全局级热切换（§8.4）：按最新配置重建注册表，写切换审计。"""
    registry = _registry(request)
    before = registry.snapshot()
    delta = _apply_config(request, session, scope="global",
                          old=",".join(before), new=",".join(registry.snapshot()),
                          source="api_reload")
    session.commit()
    return {"providers": registry.names(), **delta}


@router.patch("/routing")
def patch_routing(patch: RoutingPatch, request: Request,
                  session: Session = Depends(get_session)) -> dict:
    """档位级热切换（§8.4）。改动一并写入用户配置，重启后仍然有效。"""
    router_obj = request.app.state.router
    ai_registry = _registry(request)
    old = router_obj.as_config().get(patch.tier)
    candidates = [{"provider": c.provider, "model": c.model} for c in patch.candidates]
    try:
        router_obj.update_tier(
            patch.tier,
            [RouteCandidate(provider=c["provider"], model=c["model"]) for c in candidates],
            ai_registry.providers_map(),
        )
    except RoutingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    upsert_user_routing(patch.tier, candidates)
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
    _invalidate(request, name)  # 让下一次 health() 立即重新探测（FIX-04）
    return {"provider": name, "stored": True}


@router.get("/provider-types")
def provider_types() -> dict:
    """可用 provider 类型与能力清单，供接入表单渲染下拉项。"""
    return {
        "types": sorted(PROVIDER_TYPES),
        "capabilities": sorted(KNOWN_CAPABILITIES),
    }
