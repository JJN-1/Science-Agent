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


# ── 健康三态（FIX-04）─────────────────────────────
# 旧实现把「未配置 Key」与「不可用」混为一谈：health() 返回 False 的 provider 会被
# 注册表静默剔除，随后 Router 报「引用了不存在的 provider」——应用直接起不来，
# 且错误完全不指向真正原因（缺 Key）。三态语义把「没配好」与「坏了」分开报告。
HEALTH_OK = "ok"                      # 可正常调用
HEALTH_UNCONFIGURED = "unconfigured"  # 配置存在但缺少凭据等，需用户补全
HEALTH_DOWN = "down"                  # 探测失败 / 显式不可用

HEALTH_STATES = (HEALTH_OK, HEALTH_UNCONFIGURED, HEALTH_DOWN)


class ChatProvider(ABC):
    """Provider 协议：声明能力与价格，health() 返回三态健康状态（§8.1）。

    注册表**不再因健康状态剔除** provider：健康只是元数据，能否调用在调用点判定。
    """

    name: str
    model: str
    vendor: str
    capabilities: frozenset[str]  # json_object / tools / stream
    price: dict  # {"input": 每1k token, "output": 每1k token}

    @abstractmethod
    def complete(self, request: ChatRequest) -> ChatResponse:
        """同步补全，失败抛 ProviderError 子类。"""

    @abstractmethod
    def health(self) -> str:
        """健康检查（轻量、快速返回），返回 HEALTH_OK / HEALTH_UNCONFIGURED / HEALTH_DOWN。"""

    def unavailable_reason(self) -> str:
        """不可调用时的可操作提示，交由上层直接展示给用户。"""
        return f"provider {self.name} 当前不可用"

    def cost_of(self, prompt_tokens: int, completion_tokens: int) -> float:
        return prompt_tokens / 1000 * self.price.get("input", 0.0) + (
            completion_tokens / 1000 * self.price.get("output", 0.0)
        )


def resolve_models(name: str, cfg: dict) -> list[str]:
    """从配置解析模型清单：``models`` 优先，兼容旧的单数 ``model``。

    provider 只承载「声明了哪些模型」；具体用哪个由档位路由的 (provider, model) 决定。
    """
    raw = cfg.get("models")
    if raw is None:
        single = cfg.get("model")
        raw = [single] if single else []
    models = [str(m).strip() for m in raw if str(m).strip()]
    if not models:
        raise ValueError(f"provider {name} 未声明任何模型（需要 models 列表）")
    return models


def monotonic() -> float:
    """进程内单调时钟：测延迟、算 TTL 都该用它。"""
    return time.monotonic()


def wall_clock() -> float:
    """跨进程可比的挂钟时间戳，仅用于需要落库的时间差。

    熔断的 ``opened_at`` 必须用它：``monotonic()`` 的零点随进程而变，
    落库再读回来得到的是毫无意义的差值，冷却期判定会整个失效。
    """
    return time.time()
