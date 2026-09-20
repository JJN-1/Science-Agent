"""JSON Schema 子集校验（S1 结构化输出修正）。

**为什么需要它**：模型返回「合法 JSON」不等于「符合形状」。真实事故是 S1 收到了

    {"response": "……我生成了 3 个候选研究问题……", "format": "JSON", "note": "……"}

—— 是合法 JSON，但 `questions` 取不到；旧代码 `parsed.get("questions", [])` 返回空列表，
于是写下一个**空的**候选问题集合并报 `stage.succeeded`。用户看到的是「跑了几分钟、
显示成功、但没有结果」。只判「是不是 JSON」的校验挡不住这一类错误，必须按 schema 校验形状。

**刻意只实现一个子集**（不引入 `jsonschema` 运行时依赖，遵守 Sprint 3 的「无新增运行时依赖」约定）：
`type` / `properties` / `required` / `items` / `enum` / `minItems` / `minLength` / `minimum` / `maximum`。
未识别的关键字一律忽略（schema 由我们自己编写，不是外部输入，宽松处理即可）。
"""
from __future__ import annotations

from typing import Any

__all__ = ["schema_errors", "schema_json"]

_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "number": (int, float),
    "integer": (int,),
    "null": (type(None),),
}


def schema_json(schema: dict) -> str:
    """把 schema 序列化成给模型看的紧凑文本（ensure_ascii=False 保留中文）。"""
    import json

    return json.dumps(schema, ensure_ascii=False)


def schema_errors(instance: Any, schema: dict, path: str = "$") -> list[str]:
    """校验实例是否符合 schema，返回错误描述列表（空列表 = 通过）。

    ``path`` 是给错误信息用的定位串，形如 ``$.questions[2].score``。
    """
    if not isinstance(schema, dict):
        return []
    errors: list[str] = []

    expected = schema.get("type")
    if expected is not None:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(instance, name) for name in allowed):
            return [f"{path} 期望类型 {'/'.join(map(str, allowed))}，实际为 {_type_name(instance)}"]

    if "enum" in schema and instance not in schema["enum"]:
        return [f"{path} 取值不在允许集合 {schema['enum']!r} 中，实际为 {instance!r}"]

    if isinstance(instance, dict):
        errors += _object_errors(instance, schema, path)
    elif isinstance(instance, list):
        errors += _array_errors(instance, schema, path)
    elif isinstance(instance, str):
        errors += _string_errors(instance, schema, path)
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        errors += _number_errors(instance, schema, path)
    return errors


def _matches_type(value: Any, name: str) -> bool:
    checks = _TYPE_CHECKS.get(name)
    if checks is None:
        return True  # 未知类型名不判失败
    if name in ("number", "integer") and isinstance(value, bool):
        return False  # JSON 里 true/false 不是数字
    return isinstance(value, checks)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _object_errors(instance: dict, schema: dict, path: str) -> list[str]:
    errors: list[str] = []
    missing = [key for key in schema.get("required", []) if key not in instance]
    if missing:
        got = ", ".join(sorted(instance)) or "（空对象）"
        errors.append(f"{path} 缺少必需字段 {'、'.join(missing)}（实际字段：{got}）")
    properties = schema.get("properties") or {}
    for key, subschema in properties.items():
        if key in instance:
            errors += schema_errors(instance[key], subschema, f"{path}.{key}")
    return errors


def _array_errors(instance: list, schema: dict, path: str) -> list[str]:
    errors: list[str] = []
    min_items = schema.get("minItems")
    if isinstance(min_items, int) and len(instance) < min_items:
        errors.append(f"{path} 至少需要 {min_items} 项，实际 {len(instance)} 项")
    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(instance):
            errors += schema_errors(item, items, f"{path}[{index}]")
    return errors


def _string_errors(instance: str, schema: dict, path: str) -> list[str]:
    errors: list[str] = []
    min_length = schema.get("minLength")
    if isinstance(min_length, int) and len(instance) < min_length:
        errors.append(f"{path} 长度至少 {min_length}，实际 {len(instance)}")
    return errors


def _number_errors(instance: float, schema: dict, path: str) -> list[str]:
    errors: list[str] = []
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and instance < minimum:
        errors.append(f"{path} 不得小于 {minimum}，实际 {instance}")
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and instance > maximum:
        errors.append(f"{path} 不得大于 {maximum}，实际 {instance}")
    return errors
