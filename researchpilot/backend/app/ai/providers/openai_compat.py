from __future__ import annotations

import random
import time

import httpx
import keyring

from app.ai.base import (
    HEALTH_DOWN,
    HEALTH_OK,
    HEALTH_UNCONFIGURED,
    ChatProvider,
    ChatRequest,
    ChatResponse,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    monotonic,
    resolve_models,
)

KEYRING_SERVICE = "ResearchPilot"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 健康探测结果缓存时长（秒）：health() 会发起真实网络请求，
# 设置页每次打开都重新探测会让界面卡住数秒（§11.4 的轻量要求）。
HEALTH_PROBE_TTL_S = 30.0


class OpenAICompatProvider(ChatProvider):
    """OpenAI 兼容端点（DeepSeek/智谱/硅基流动/OpenAI 等，§8.1）。

    - 超时 120s（§11.4）；指数退避 + 抖动重试 ≤3 次；参数错误不重试
    - API Key 经 keyring 读 Windows 凭据管理器，config 只存 api_key_ref
    - transport 参数仅供测试注入 httpx.MockTransport
    """

    def __init__(self, name: str, cfg: dict, transport: httpx.BaseTransport | None = None) -> None:
        self.name = name
        self.models = resolve_models(name, cfg)
        self.model = self.models[0]
        self.vendor = cfg.get("vendor", "openai")
        self.capabilities = frozenset(cfg.get("capabilities", ["json_object", "tools"]))
        self.price = cfg.get("price") or cfg.get("price_per_1k") or {"input": 0.0, "output": 0.0}
        self.base_url = cfg["base_url"].rstrip("/")
        self.timeout_s = float(cfg.get("timeout_s", 120))
        self.max_retries = int(cfg.get("max_retries", 3))
        self.backoff_base = float(cfg.get("backoff_base", 1.0))
        ref = cfg.get("api_key_ref")
        self.api_key_ref = name if ref is None else str(ref).strip()
        # api_key_ref 显式留空 = 该端点无需鉴权（本地自建端点）
        self.auth_required = bool(self.api_key_ref)
        self.extra_headers = {str(k): str(v) for k, v in (cfg.get("extra_headers") or {}).items()}
        self.extra_body = dict(cfg.get("extra_body") or {})
        self._transport = transport
        self._health_cache: tuple[float, str] | None = None

    def _peek_key(self) -> str | None:
        """读取凭据；缺失或凭据后端不可用时返回 None，不抛异常。"""
        if not self.auth_required:
            return None
        try:
            return keyring.get_password(KEYRING_SERVICE, self.api_key_ref) or None
        except Exception:
            return None

    def _api_key(self) -> str | None:
        if not self.auth_required:
            return None
        key = self._peek_key()
        if not key:
            raise ProviderUnavailable(
                f"provider {self.name} 未配置 API Key（凭据管理器引用: {self.api_key_ref}）"
            )
        return key

    def _headers(self) -> dict[str, str]:
        key = self._api_key()
        headers: dict[str, str] = {"Authorization": f"Bearer {key}"} if key else {}
        headers.update(self.extra_headers)  # 本地端点可用自定义头替代 Bearer 鉴权
        return headers

    def complete(self, request: ChatRequest) -> ChatResponse:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.schema and "json_object" in self.capabilities:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(payload)
        choice = (data.get("choices") or [{}])[0].get("message", {})
        usage = data.get("usage", {})
        return ChatResponse(
            text=choice.get("content", ""),
            provider=self.name,
            model=data.get("model", self.model),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )

    def _post(self, payload: dict) -> dict:
        headers = self._headers()
        body = {**payload, **self.extra_body}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(
                    base_url=self.base_url, timeout=self.timeout_s,
                    transport=self._transport, follow_redirects=False,
                ) as client:
                    resp = client.post("/chat/completions", json=body, headers=headers)
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code in RETRYABLE_STATUS:
                    if resp.status_code == 429 and attempt == self.max_retries:
                        raise RateLimited(f"{self.name} 持续限流 (429)")
                    last_error = ProviderUnavailable(f"{self.name} HTTP {resp.status_code}")
                else:
                    raise ProviderError(
                        f"{self.name} HTTP {resp.status_code}: {resp.text[:200]}"
                    )
            except httpx.TimeoutException:
                last_error = ProviderUnavailable(f"{self.name} 请求超时")
            except httpx.HTTPError as exc:
                last_error = ProviderUnavailable(f"{self.name} 网络错误: {exc}")
            if attempt < self.max_retries:
                time.sleep(self.backoff_base * (2**attempt) + random.uniform(0, 0.5))
        raise last_error or ProviderUnavailable(f"{self.name} 调用失败")

    def health(self) -> str:
        """三态健康（FIX-04）：ok / unconfigured / down，结果短期缓存。

        「没录 Key」必须是 unconfigured 而不是 down —— 否则注册表会把它当作坏后端
        剔除，用户看到的会是「引用了不存在的 provider」这种指错方向的启动错误。
        """
        if self._health_cache is not None:
            checked_at, state = self._health_cache
            if monotonic() - checked_at < HEALTH_PROBE_TTL_S:
                return state
        state = self._probe_health()
        self._health_cache = (monotonic(), state)
        return state

    def _probe_health(self) -> str:
        if self.auth_required and self._peek_key() is None:
            return HEALTH_UNCONFIGURED
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=5.0, transport=self._transport,
                follow_redirects=False,
            ) as client:
                resp = client.get("/models", headers=self._headers())
            return HEALTH_OK if resp.status_code < 500 else HEALTH_DOWN
        except httpx.HTTPError:
            return HEALTH_DOWN

    def invalidate_health(self) -> None:
        """录入 / 更换 Key 后调用，使下一次 health() 立即重新探测。"""
        self._health_cache = None

    def list_remote_models(self, base_url: str | None = None,
                           api_key: str | None = None) -> list[str]:
        """探测端点可用模型（US-312）；只取 `id` 字段。

        上游清单里没有能力与价格信息，因此返回值只用于给用户勾选模型 ID，
        绝不据此推断 capabilities / price（设计 §8.1）。
        """
        url = (base_url or self.base_url).rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else self._headers()
        with httpx.Client(timeout=10.0, transport=self._transport,
                          follow_redirects=False) as client:
            resp = client.get(f"{url}/models", headers=headers)
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name} 探测模型失败 HTTP {resp.status_code}")
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ProviderError(f"{self.name} 的 /models 响应不是 OpenAI 兼容格式")
        return [str(item["id"]) for item in items
                if isinstance(item, dict) and item.get("id")]

    def unavailable_reason(self) -> str:
        if self.auth_required and self._peek_key() is None:
            return (
                f"provider {self.name} 尚未录入 API Key"
                f"（凭据管理器引用: {self.api_key_ref}）。"
                f"请打开「设置 · 模型后端」录入后重试。"
            )
        return (
            f"provider {self.name} 当前不可达（{self.base_url}）。"
            f"请检查网络、base_url 与 Key 是否有效。"
        )
