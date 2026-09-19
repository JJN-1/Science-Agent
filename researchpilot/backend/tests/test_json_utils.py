from __future__ import annotations

import pytest

from app.ai.json_utils import extract_json, is_json


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        # ```json 围栏（真实模型最典型的输出形态）
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('```JSON\r\n{"a": 1}\r\n```', {"a": 1}),
        # 前后带自然语言
        ('好的，结果如下：\n{"a": 1}\n希望对你有帮助。', {"a": 1}),
        # 尾随逗号
        ('{"a": 1,}', {"a": 1}),
        ('{"items": [1, 2,]}', {"items": [1, 2]}),
        # 整体单引号
        ("{'a': 1}", {"a": 1}),
        # 顶层数组
        ('[{"a": 1}]', [{"a": 1}]),
        # 字符串里含大括号，扫描时不能提前结束
        ('{"text": "含 } 与 { 的字符串"}', {"text": "含 } 与 { 的字符串"}),
        # 转义引号
        ('前言 {"a": "he said \\"hi\\""} 后缀', {"a": "he said \"hi\""}),
    ],
)
def test_extract_json_tolerates_real_world_output(raw, expected):
    assert extract_json(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "not json at all", "still not json"])
def test_extract_json_rejects_non_json(raw):
    with pytest.raises(ValueError):
        extract_json(raw)


def test_extract_json_error_message_carries_raw_output():
    """解析失败要能带上原始输出，否则轨迹里查不出模型到底吐了什么。"""
    with pytest.raises(ValueError, match="模型输出片段|无法从模型输出"):
        extract_json("完全不是 JSON")


def test_is_json_wrapper():
    assert is_json('```json\n{"a": 1}\n```') is True
    assert is_json("nope") is False
