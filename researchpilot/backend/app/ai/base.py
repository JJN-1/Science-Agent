from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.ai.json_utils import extract_json

#: 对话角色。``tool`` 是**工具结果的回程**：模型发起调用后，执行结果必须以这个角色
#: 送回，否则模型无从知道工具跑出了什么，会反复发起同一次调用。
CHAT_ROLES = ("system", "user", "assistant", "tool")


class ToolArgumentsError(Exception):
    """工具调用的参数无法解析（US-409）。

    **绝不静默降级成空参数**：把「模型给的参数看不懂」当成「这次调用没有参数」，
    工具会带着默认行为跑起来 —— 用户看到的是「工具执行成功了」，而实际上它执行的是
    一件与模型意图不同的事。这类错误的代价远高于一次明确的失败。
    """

    code = "LLM-TOOLS-001"

    def __init__(self, *args: object, raw_output: str = "") -> None:
        super().__init__(*args)
        self.raw_output = raw_output


def parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    """把工具调用的参数从 JSON 字符串解析为对象。

    ``arguments`` 在 OpenAI 协议里**是 JSON 字符串而不是对象**，这一点是本层最容易被
    忽略的细节：直接把它当 dict 用会在第一次真实工具调用时炸掉。

    - ``None`` / 空串 / 纯空白 → ``{}``（无参数是**合法**的，很多工具就长这样）
    - 非空但解析不出、或解析出非对象 → 抛 ``ToolArgumentsError``（附原文片段）
    """
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = extract_json(raw)
    except ValueError as exc:
        raise ToolArgumentsError(
            f"LLM-TOOLS-001: 工具参数不是合法 JSON（{exc}）；原文片段: {raw[:200]!r}",
            raw_output=raw,
        ) from exc
    if not isinstance(parsed, dict):
        raise ToolArgumentsError(
            f"LLM-TOOLS-001: 工具参数必须是 JSON 对象，实际是 {type(parsed).__name__}；"
            f"原文片段: {raw[:200]!r}",
            raw_output=raw,
        )
    return parsed


@dataclass
class ToolCall:
    """一次工具调用（模型提出，系统执行）。

    ``arguments`` **保持上游给的原始字符串**，不在这一层解析：解析失败要能在调用点
    被看见并处置（``parse_arguments()``），而不是在响应解析时被吞掉。字段名与 OpenAI
    的 ``function.name`` / ``function.arguments`` 的扁平化结果一致。
    """

    id: str
    name: str
    arguments: str = ""

    def parse_arguments(self) -> dict[str, Any]:
        return parse_tool_arguments(self.arguments)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, raw: Any) -> ToolCall:
        if not isinstance(raw, dict):
            raise ToolArgumentsError(f"LLM-TOOLS-001: 工具调用必须是对象，实际为 {type(raw).__name__}")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ToolArgumentsError("LLM-TOOLS-001: 工具调用缺少 name")
        arguments = raw.get("arguments")
        if arguments is not None and not isinstance(arguments, str):
            # 少数端点会直接把 arguments 给成对象。宽容地序列化回去，
            # 但**不**在响应解析阶段解析它 —— 解析失败留给调用点处置。
            arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        return cls(
            id=str(raw.get("id") or "").strip(),
            name=name,
            arguments=arguments or "",
        )


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    #: ``role="tool"`` 时指向它回应的那次调用。端点靠它把结果对回调用，
    #: 缺失或对不上会被直接拒收整条请求。
    tool_call_id: str | None = None
    #: ``role="assistant"`` 且该轮调用了工具时，**必须**带回来一起回放：
    #: 只发 tool 结果而没有它所回应的那次调用，请求同样会被拒收。
    tool_calls: list[ToolCall] | None = None


@dataclass
class ChatRequest:
    messages: list[ChatMessage]
    tier: str = "extract"
    schema: dict | None = None  # JSON Schema（结构化输出要求）
    max_tokens: int = 1024
    temperature: float = 0.7
    # 本次调用用哪个模型。由档位路由的 (provider, model) 决定；留空则用 provider 的
    # 首个声明模型。provider 承载「声明了哪些模型」，具体用哪个是路由的事（§8.4）。
    model: str | None = None
    #: 可用工具清单（OpenAI 的 ``tools`` 结构：``[{"type":"function","function":{...}}]``）。
    #: ``None`` = 本次不带工具，与 ``[]``（明确「没有可用工具」）不同。
    tools: list[dict] | None = None
    #: ``"auto"`` / ``"none"`` / ``"required"``，或指定某个函数对象。``None`` 表示不传，
    #: 由端点自行决定缺省行为。
    tool_choice: str | dict | None = None


@dataclass
class ChatResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    degraded: list[str] = field(default_factory=list)
    # 实际发出的 HTTP 尝试次数（含重试）。1 = 一次就成功。
    attempts: int = 1
    #: 模型提出的工具调用。非空即表示**这一轮模型选择了调工具**（此时 ``text`` 通常为空），
    #: 因此结构化输出的校验对这类响应不适用（见 ``degrade``）。
    tool_calls: list[ToolCall] | None = None


class ProviderError(Exception):
    """AI 接入层错误基类，code 遵循 <域>-<类别>-<序号>。

    ``raw_output`` 保存上游返回的原始文本（模型正文，或端点的错误响应体；拿不到时为空串）。
    只报「不符合 schema」或「HTTP 403」不足以排查——得看上游究竟回了什么。把原文挂在
    异常上，轨迹写入点（``StageContext.llm``）就能把它一并落进 ``agent_steps``，
    而不是任由现场随异常栈一起消失（FIX-06 的保证要对**所有**失败路径成立）。

    ``attempts`` 是失败前实际发出的尝试次数：默认超时 120s × 4 次尝试意味着一次调用
    最长可拖近 8 分钟，「等了很久」到底是不是重试拖出来的，只有这个数字说得清。

    ``provider`` / ``model`` 由降级链在捕获时补上（provider 自己不知道被谁调度）：
    失败记账要落到具体后端名下，否则统计里出现的是一行「unknown」。
    """

    code = "LLM-PROVIDER-001"

    def __init__(self, *args: object, raw_output: str = "", attempts: int = 1,
                 provider: str = "", model: str = "") -> None:
        super().__init__(*args)
        self.raw_output = raw_output
        self.attempts = attempts
        self.provider = provider
        self.model = model


class ProviderUnavailable(ProviderError):
    code = "LLM-UNAVAIL-001"


class RateLimited(ProviderError):
    code = "LLM-QUOTA-001"


class QuotaExceeded(ProviderError):
    code = "LLM-QUOTA-002"


class ToolCapabilityMissing(ProviderError):
    """请求带工具，但该 provider 未声明 ``tools`` 能力（US-409）。

    **单独分类的理由**：这是**配置不匹配**，不是后端故障。若与普通失败一起计入熔断，
    一个「没配 tools 的后端被某个带工具的档位引用」的配置错误，会连累它在**别的档位**
    上被判定为不可用 —— 故障会横向扩散到与它无关的调用上。因此降级链只跳过它，
    不记失败。
    """

    code = "LLM-TOOLS-002"


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

    ``name`` 是**身份**（= 配置文件的键，路由与凭据引用都认它，创建后不变），
    ``label`` 是**展示名**（用户可改，可中文，可重复）。分开之后「改个名字」不再
    等于「删掉重建」。
    """

    name: str
    label: str
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

    def credential_view(self) -> dict:
        """凭据状态，供设置页回填：引用名 + 是否必须鉴权 + 是否已录入。

        旧实现里设置页拿不到这三项，于是编辑表单只能把 Key 一栏留空、
        并且无法知道这个端点其实是「无需鉴权」—— 一次编辑就把配置改坏了。
        """
        return {"api_key_ref": None, "auth_required": False, "has_key": False}

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
