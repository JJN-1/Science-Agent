from __future__ import annotations

import random
import time

import httpx
import keyring

from app.ai.base import (
    ChatProvider,
    ChatRequest,
    ChatResponse,
    ProviderError,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)

KEYRING_SERVICE = "ResearchPilot"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OpenAICompatProvider(ChatProvider):
    """OpenAI 兼容端点（DeepSeek/智谱/硅基流动/OpenAI 等，§8.1）。

    - 超时 120s（§11.4）；指数退避 + 抖动重试 ≤3 次；参数错误不重试
    - API Key 经 keyring 读 Windows 凭据管理器，config 只存 api_key_ref
    - transport 参数仅供测试注入 httpx.MockTransport
    """

    def __init__(self, name: str, cfg: dict, transport: httpx.BaseTransport | None = None) -> None:
        self.name = name
        self.model = cfg["model"]
        self.vendor = cfg.get("vendor", "openai")
        self.capabilities = frozenset(cfg.get("capabilities", ["json_object", "tools"]))
        self.price = cfg.get("price_per_1k", {"input": 0.0, "output": 0.0})
        self.base_url = cfg["base_url"].rstrip("/")
        self.timeout_s = float(cfg.get("timeout_s", 120))
        self.max_retries = int(cfg.get("max_retries", 3))
        self.backoff_base = float(cfg.get("backoff_base", 1.0))
        self.api_key_ref = cfg.get("api_key_ref", name)
        self._transport = transport

    def _api_key(self) -> str:
        key = keyring.get_password(KEYRING_SERVICE, self.api_key_ref)
        if not key:
            raise ProviderUnavailable(
                f"provider {self.name} 未配置 API Key（凭据管理器引用: {self.api_key_ref}）"
            )
        return key

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
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(
                    base_url=self.base_url, timeout=self.timeout_s,
                    transport=self._transport, follow_redirects=False,
                ) as client:
                    resp = client.post("/chat/completions", json=payload, headers=headers)
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

    def health(self) -> bool:
        try:
            key = self._api_key()
        except ProviderError:
            return False
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=5.0, transport=self._transport,
                follow_redirects=False,
            ) as client:
                resp = client.get("/models", headers={"Authorization": f"Bearer {key}"})
            return resp.status_code < 500
        except httpx.HTTPError:
            return False
