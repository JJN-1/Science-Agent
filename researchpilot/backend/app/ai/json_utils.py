from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["extract_json"]

# ```json ... ``` / ``` ... ``` 代码围栏
_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n?(.*?)```", re.DOTALL)
# 尾随逗号：{"a": 1,} / [1, 2,]
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _strip_fences(text: str) -> str:
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _first_balanced(text: str, opener: str, closer: str) -> str | None:
    """截出第一个括号平衡的子串，正确跳过字符串字面量与转义字符。"""
    start = text.find(opener)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _light_repair(text: str) -> str:
    """轻量修复：丢掉尾随逗号（真实模型最常见的 JSON 瑕疵）。"""
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def _repair_single_quotes(text: str) -> str | None:
    """整段用单引号包裹的兜底修复。

    仅在完全不含双引号时启用，避免破坏字符串内容里合法的撇号。
    """
    if '"' in text or "'" not in text:
        return None
    return text.replace("'", '"')


def extract_json(text: str) -> Any:
    """从模型输出中稳健地提取 JSON（FIX-06）。

    真实模型经常会输出 ```json 围栏、加一句前言/后缀，或带尾随逗号。裸调
    ``json.loads`` 会让这些完全正常的输出被判为 LLM-SCHEMA-001 而失败。

    依次尝试：原文 → 剥离代码围栏 → 提取首个平衡的 ``{}`` / ``[]`` → 轻量修复。
    全部失败时抛 ``ValueError``（附原始输出片段，便于从轨迹里排查）。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("模型输出为空，无法解析 JSON")

    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)
    unfenced = _strip_fences(text)
    if unfenced != stripped:
        candidates.append(unfenced)
    for base in list(candidates):
        for opener, closer in (("{", "}"), ("[", "]")):
            chunk = _first_balanced(base, opener, closer)
            if chunk:
                candidates.append(chunk)

    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in ordered:
            ordered.append(candidate)

    for candidate in ordered:
        variants = [candidate, _light_repair(candidate)]
        repaired = _repair_single_quotes(_light_repair(candidate))
        if repaired is not None:
            variants.append(repaired)
        for variant in variants:
            try:
                return json.loads(variant)
            except (ValueError, TypeError):
                continue

    raise ValueError(f"无法从模型输出中解析出 JSON：{text[:200]!r}")


def is_json(text: str) -> bool:
    """校验用包装：extract_json 成功即视为合法。"""
    try:
        extract_json(text)
        return True
    except ValueError:
        return False
