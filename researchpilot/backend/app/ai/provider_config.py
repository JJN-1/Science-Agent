"""用户自定义 Provider 接入的配置校验与规范化（US-312 / 设计 §8.1）。

用户在设置页自行接入任意 OpenAI 兼容 / Anthropic 兼容 / 本地自建端点，不改代码、不重启。
本模块是那条路径的唯一入口：任何来自 API 的 provider 配置都必须先过 `normalize()`，
再写进用户 config.yaml，最后交给 `ProviderRegistry` 建实例。

几个刻意的取舍：

- ``models`` 是列表（canonical），旧的单数 ``model`` 仍被接受并归一为单元素列表。
- ``price`` 是 canonical（旧的 ``price_per_1k`` 仍被接受）；价格**只取本地配置**，
  绝不从上游模型清单推断——清单里根本没有价格字段。
- ``capabilities`` 同理，只认本地声明，未声明按缺失处理（设计 §8.3）。
- ``base_url`` 的地址校验挡的是链路本地与未指定地址；回环地址放行，
  因为「本地自建端点」是设计 §4.2 明确支持的形态（本机跑 Ollama / vLLM）。
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlparse

from app.ai.registry import PROVIDER_TYPES

KNOWN_CAPABILITIES = frozenset({"json_object", "tools", "stream", "vision"})
DEFAULT_CAPABILITIES = ("json_object",)
DEFAULT_TIMEOUT_S = 120.0
MAX_LABEL_LEN = 64

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# 与 provider 实例约定的配置键（写回 config.yaml 时只写这些）
CONFIG_KEYS = (
    "name", "type", "base_url", "models", "vendor", "api_key_ref",
    "capabilities", "price", "timeout_s", "extra_headers", "extra_body",
)


class ProviderConfigError(ValueError):
    code = "CFG-PROVIDER-001"


def generate_id(label: str, taken: Iterable[str]) -> str:
    """从展示名派生一个稳定的 provider id（= 配置文件里的键）。

    **身份与展示名解耦**：id 创建后不再变，档位路由 / 凭据引用 / 迁移统计都认它，
    用户改名称不会打断任何一条引用。旧实现把名字本身当身份，于是「改名」等于
    「删掉重建」—— 路由断、Key 找不到、历史统计断成两截。
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(label or "")).strip("-").lower()[:48]
    if not NAME_PATTERN.match(slug):  # 纯中文名会退化成空串
        slug = "provider"
    existing = set(taken)
    if slug not in existing:
        return slug
    n = 2
    while f"{slug}-{n}" in existing:
        n += 1
    return f"{slug}-{n}"


def normalize(provider_id: str, raw: dict | None) -> dict:
    """校验并规范化一条 provider 配置；不合法一律抛 ProviderConfigError（带可读原因）。

    ``provider_id`` 是身份（配置键），``raw["name"]`` 是可改的展示名。
    """
    if not NAME_PATTERN.match(provider_id or ""):
        raise ProviderConfigError(
            f"provider id 非法: {provider_id!r}（仅允许字母/数字/下划线/连字符/点，"
            f"1–64 位，且不以符号开头）"
        )
    cfg = dict(raw or {})

    ptype = cfg.get("type")
    if ptype not in PROVIDER_TYPES:
        supported = ", ".join(sorted(PROVIDER_TYPES))
        raise ProviderConfigError(f"未知 provider 类型: {ptype!r}；已支持: {supported}")

    out: dict = {
        "name": _normalize_label(cfg.get("name"), provider_id),
        "type": ptype,
        "models": _normalize_models(cfg),
        "vendor": _normalize_vendor(cfg),
        "capabilities": _normalize_capabilities(cfg),
        "price": _normalize_price(cfg.get("price", cfg.get("price_per_1k"))),
        "timeout_s": _normalize_timeout(cfg.get("timeout_s")),
    }

    if ptype != "mock":
        out["base_url"] = normalize_base_url(cfg.get("base_url"))
        # 留空表示该端点无需鉴权（本地自建），缺省则引用名即 provider id
        ref = cfg.get("api_key_ref")
        out["api_key_ref"] = provider_id if ref is None else str(ref).strip()

    for key in ("extra_headers", "extra_body"):
        value = cfg.get(key)
        if value in (None, {}):
            continue
        if not isinstance(value, dict):
            raise ProviderConfigError(f"{key} 必须是字典")
        out[key] = {str(k): v for k, v in value.items()}

    return out


def _normalize_label(raw: object, fallback: str) -> str:
    """展示名：可改、可中文、可重复，与身份无关。"""
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return fallback
    if len(text) > MAX_LABEL_LEN:
        raise ProviderConfigError(f"名称过长（最多 {MAX_LABEL_LEN} 字符）")
    if any(ch in text for ch in "\r\n\t"):
        raise ProviderConfigError("名称不得包含换行或制表符")
    return text


def normalize_base_url(raw: object) -> str:
    """校验 base_url 并去掉尾部斜杠。"""
    text = str(raw).strip() if raw is not None else ""
    if not text:
        raise ProviderConfigError("base_url 必填")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise ProviderConfigError("base_url 必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise ProviderConfigError("base_url 缺少主机名")
    if parsed.username or parsed.password:
        raise ProviderConfigError("base_url 不得内嵌用户名/密码")
    if parsed.query or parsed.fragment:
        raise ProviderConfigError("base_url 不得带查询串或片段")
    reason = internal_host_reason(parsed.hostname)
    if reason:
        raise ProviderConfigError(f"base_url 主地址不可用：{reason}")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def internal_host_reason(host: str) -> str | None:
    """返回拒绝原因；放行则返回 None。

    放行回环地址（本机自建端点），拒绝链路本地 / 未指定 / 组播 / 保留段——
    这些地址除了做 SSRF 跳板外没有正常的模型端点用途。
    """
    lowered = host.strip().strip("[]").lower()
    if not lowered:
        return "主机名为空"
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return None  # 域名交由 DNS 处理，这里不做解析
    if ip.is_loopback:
        return None
    if ip.is_link_local:
        return f"{ip} 属于链路本地网段（云元数据服务即在此段）"
    if ip.is_unspecified:
        return f"{ip} 是未指定地址"
    if ip.is_multicast:
        return f"{ip} 是组播地址"
    if ip.is_reserved:
        return f"{ip} 属于保留网段"
    return None


def _normalize_models(cfg: dict) -> list[str]:
    raw = cfg.get("models")
    if raw is None:
        single = cfg.get("model")
        raw = [single] if single else []
    if not isinstance(raw, list):
        raise ProviderConfigError("models 必须是模型 ID 列表")
    models = [str(m).strip() for m in raw if str(m).strip()]
    if not models:
        raise ProviderConfigError("models 不能为空（至少声明一个模型 ID）")
    if len(set(models)) != len(models):
        raise ProviderConfigError("models 中存在重复的模型 ID")
    return models


def _normalize_vendor(cfg: dict) -> str:
    vendor = str(cfg.get("vendor") or "").strip()
    if not vendor:
        raise ProviderConfigError("vendor 必填（critique 档据此做跨厂商校验）")
    return vendor


def _normalize_capabilities(cfg: dict) -> list[str]:
    raw = cfg.get("capabilities")
    if raw is None:
        return list(DEFAULT_CAPABILITIES)
    if not isinstance(raw, list):
        raise ProviderConfigError("capabilities 必须是列表")
    caps = {str(c).strip() for c in raw if str(c).strip()}
    unknown = sorted(caps - KNOWN_CAPABILITIES)
    if unknown:
        supported = ", ".join(sorted(KNOWN_CAPABILITIES))
        raise ProviderConfigError(f"未知能力: {', '.join(unknown)}；可选: {supported}")
    return sorted(caps)


def _normalize_price(raw: object) -> dict:
    if raw is None:
        return {"input": 0.0, "output": 0.0}
    if not isinstance(raw, dict):
        raise ProviderConfigError("price 必须是 {input, output} 形式的字典")
    out: dict[str, float] = {}
    for side in ("input", "output"):
        try:
            value = float(raw.get(side, 0.0))
        except (TypeError, ValueError):
            raise ProviderConfigError(f"price.{side} 必须是数字") from None
        if value < 0:
            raise ProviderConfigError(f"price.{side} 不能为负")
        out[side] = value
    return out


def _normalize_timeout(raw: object) -> float:
    if raw is None:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ProviderConfigError("timeout_s 必须是数字") from None
    if value <= 0:
        raise ProviderConfigError("timeout_s 必须大于 0")
    return value


def public_view(provider_id: str, cfg: dict) -> dict:
    """给 API 响应用的视图（与配置文件字段一致，便于用户对照 config.yaml）。

    同时给出 ``id``（身份，不可改）与 ``name``（展示名，可改）——
    设置页据此渲染「标识 + 名称」两个字段，重命名不再需要删掉重建。
    """
    return {"id": provider_id, **cfg, "name": cfg.get("name") or provider_id}
