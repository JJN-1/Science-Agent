"""token 估算（D11：自写启发式，不引 ``tiktoken``）。

引 tokenizer 会让安装包体积、离线可用性与启动时间同时变差，而这里要的只是一个
**用于裁剪决策**的数字，不是账单。刻意保持在「宁可偏高」的一侧：
估算偏低会让上下文静默超窗，表现为上游 400 或模型忘了刚说过的话；
估算偏高只是少塞两轮对话，用户感知不到。

定价/记账走另一条路（``llm_usage`` 存上游返回的真实用量），两者不复用。
"""
from __future__ import annotations

import re

#: 每条消息的固定开销（角色标记、`<|im_start|>` 之类的分隔符）
MESSAGE_OVERHEAD_TOKENS = 4

#: 非 CJK 文本的经验比值（英文/代码约 3–4 字符/token，取 3 以偏高）
NON_CJK_CHARS_PER_TOKEN = 3

_CJK_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)
_WHITESPACE = re.compile(r"\s+")


def estimate_tokens(text: str | None) -> int:
    """估算一段文本的 token 数。

    规则（刻意粗粒度，可预测比精确更重要）：

    - CJK 字符（中日韩）按 **1 字符 = 1 token**（多数 tokenizer 的实际区间是
      0.6–1.5，取 1 落在中位偏保守）
    - 其余字符按 **3 字符 = 1 token**（英文与代码的经验值，向上取整）
    - 连续空白先折叠成单个空格，避免缩进与换行把估算吹起来
    """
    if not text:
        return 0
    collapsed = _WHITESPACE.sub(" ", text)
    cjk = len(_CJK_PATTERN.findall(collapsed))
    if cjk:
        collapsed = _CJK_PATTERN.sub("", collapsed)
    rest = len(collapsed)
    return cjk + -(-rest // NON_CJK_CHARS_PER_TOKEN)


def estimate_message_tokens(role: str, content: str | None) -> int:
    """单条消息的估算：内容 + 角色名 + 固定开销。"""
    return MESSAGE_OVERHEAD_TOKENS + estimate_tokens(role) + estimate_tokens(content)
