from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ChatMessage:
    role: str  # system | user | assistant
    content: str


@dataclass
class ChatRequest:
    messages: list[ChatMessage]
    tier: str = "extract"
    schema: dict | None = None  # JSON Schema（结构化输出要求）
    max_tokens: int = 1024
    temperature: float = 0.7


@dataclass
class ChatResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    degraded: list[str] = field(default_factory=list)


class ProviderError(Exception):
    """AI 接入层错误基类，code 遵循 <域>-<类别>-<序号>。"""

    code = "LLM-PROVIDER-001"


class ProviderUnavailable(ProviderError):
    code = "LLM-UNAVAIL-001"


class RateLimited(ProviderError):
    code = "LLM-QUOTA-001"


class QuotaExceeded(ProviderError):
    code = "LLM-QUOTA-002"


class CircuitOpen(ProviderUnavailable):
    """后端处于熔断冷却期。"""

    code = "LLM-CIRCUIT-001"


class ChatProvider(ABC):
    """Provider 协议：声明能力与价格，health() 不健康不进注册表（§8.1）。"""

    name: str
    model: str
    vendor: str
    capabilities: frozenset[str]  # json_object / tools / stream
    price: dict  # {"input": 每1k token, "output": 每1k token}

    @abstractmethod
    def complete(self, request: ChatRequest) -> ChatResponse:
        """同步补全，失败抛 ProviderError 子类。"""

    @abstractmethod
    def health(self) -> bool:
        """健康检查（轻量、快速返回）。"""

    def cost_of(self, prompt_tokens: int, completion_tokens: int) -> float:
        return prompt_tokens / 1000 * self.price.get("input", 0.0) + (
            completion_tokens / 1000 * self.price.get("output", 0.0)
        )


def monotonic() -> float:
    return time.monotonic()
