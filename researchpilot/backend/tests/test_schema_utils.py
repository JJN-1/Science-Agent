from __future__ import annotations

from app.ai.schema_utils import schema_errors, schema_json

OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "minLength": 1},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["question", "score"],
            },
        }
    },
    "required": ["questions"],
}


def test_valid_instance_passes():
    ok = {"questions": [{"question": "Q", "score": 0.8}]}
    assert schema_errors(ok, OBJECT_SCHEMA) == []


def test_missing_required_field_is_reported_with_path():
    """真实事故的形状：合法 JSON，但顶层没有 questions。"""
    errors = schema_errors({"response": "……", "format": "JSON"}, OBJECT_SCHEMA)
    assert len(errors) == 1
    assert errors[0].startswith("$ 缺少必需字段 questions")
    assert "format" in errors[0] and "response" in errors[0]  # 顺便告诉实际拿到了什么


def test_empty_array_violates_min_items():
    errors = schema_errors({"questions": []}, OBJECT_SCHEMA)
    assert len(errors) == 1 and "$.questions" in errors[0] and "至少需要 1 项" in errors[0]


def test_nested_item_error_points_at_index():
    errors = schema_errors({"questions": [{"question": "Q", "score": 0.5},
                                          {"question": "Q2"}]}, OBJECT_SCHEMA)
    assert len(errors) == 1
    assert "$.questions[1] 缺少必需字段 score" in errors[0]


def test_type_mismatch_short_circuits():
    errors = schema_errors({"questions": "not-a-list"}, OBJECT_SCHEMA)
    assert len(errors) == 1 and "期望类型 array，实际为 string" in errors[0]


def test_number_range_and_bool_is_not_number():
    assert "不得大于 1" in schema_errors(
        {"questions": [{"question": "Q", "score": 1.5}]}, OBJECT_SCHEMA)[0]
    # JSON 里 true 不是数字：否则 bool 会被当成 0/1 混过数值校验
    assert "期望类型 number" in schema_errors(
        {"questions": [{"question": "Q", "score": True}]}, OBJECT_SCHEMA)[0]


def test_enum_and_unknown_keywords_are_tolerant():
    assert schema_errors("x", {"enum": ["a", "b"]}) == ["$ 取值不在允许集合 ['a', 'b'] 中，实际为 'x'"]
    # 未识别的关键字一律忽略（schema 由我们自己编写，不是外部输入）
    assert schema_errors({"a": 1}, {"type": "object", "additionalProperties": False}) == []


def test_schema_json_keeps_chinese_readable():
    assert "研究问题" in schema_json({"description": "研究问题"})
