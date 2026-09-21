"""工具契约与注册表（US-404）。

这份测试钉的是**「收口」这件事本身**，不是某个具体工具的功能：

- 白名单拒绝时，工具**一次都没被调用**（不是「调了再拦」）——「最小权限」是执行前的判断
- 参数不合 schema 时，同样不进入 ``run``，且错误里带违规路径
- 工具自己失败 → ``ok=False``（模型可见可重试，D9）；调用方违规 → 抛 ``ToolError``
- 结果超限 → 头尾保留 + 写明丢了多少 + **最终长度不超上限**（D10）

「不进入 run」这一类断言必须用**调用计数器**来证，不能只看返回值 ——
只看返回值的话，「拦住了」与「跑了但返回空」长得一模一样。
"""

from __future__ import annotations

import pytest

from app.agent_kernel.errors import ToolError
from app.agent_kernel.tools.base import (
    DANGEROUS,
    DEFAULT_RESULT_MAX_BYTES,
    EXECUTE,
    Tool,
    ToolContext,
    ToolResult,
    ToolSpec,
    truncate_tool_output,
)
from app.agent_kernel.tools.pipeline import RunPipelineTool
from app.agent_kernel.tools.registry import (
    BAD_ARGUMENTS,
    NOT_ALLOWED,
    NOT_REGISTERED,
    ToolRegistry,
)

ECHO_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1},
        "tag": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["text"],
}


class EchoTool(Tool):
    """记录调用次数，便于断言「拦下来时一次都没跑」。"""

    def __init__(self, **overrides) -> None:
        params = {
            "name": "echo", "description": "回显", "parameters": ECHO_SCHEMA,
        }
        params.update(overrides)
        self.spec = ToolSpec(**params)
        self.calls: list[dict] = []

    def run(self, args, ctx):  # noqa: ANN001
        self.calls.append(args)
        return ToolResult(ok=True, output={"echo": args["text"]})


class BoomTool(Tool):
    def __init__(self) -> None:
        self.spec = ToolSpec(name="boom", description="总是抛错", parameters={"type": "object"})

    def run(self, args, ctx):  # noqa: ANN001
        raise ValueError("工具内部炸了")


class ContractViolationTool(Tool):
    """工具自己再校验一次参数并抛 ``ToolError`` —— 不该被注册表兜成 ok=False。"""

    def __init__(self) -> None:
        self.spec = ToolSpec(name="strict", description="自校验", parameters={"type": "object"})

    def run(self, args, ctx):  # noqa: ANN001
        raise ToolError("工具内部判定的契约违规")


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(BoomTool())
    return reg


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(project_id=1)


# ── ToolSpec 自校验 ─────────────────────────────

def test_tool_spec_rejects_blank_name():
    with pytest.raises(ToolError) as exc:
        ToolSpec(name="   ", description="x", parameters={})
    assert NOT_REGISTERED in str(exc.value)


def test_tool_spec_rejects_blank_description():
    """description 是模型唯一的选择依据，空着等于让模型猜这个工具干什么。"""
    with pytest.raises(ToolError) as exc:
        ToolSpec(name="t", description="  ", parameters={})
    assert "description" in str(exc.value)


def test_tool_spec_rejects_unknown_permission():
    with pytest.raises(ToolError) as exc:
        ToolSpec(name="t", description="d", parameters={}, permission="admin")
    assert "admin" in str(exc.value)


def test_tool_spec_rejects_non_positive_timeout_and_limit():
    with pytest.raises(ToolError):
        ToolSpec(name="t", description="d", parameters={}, timeout_s=0)
    with pytest.raises(ToolError):
        ToolSpec(name="t", description="d", parameters={}, result_max_bytes=0)


def test_tool_spec_trims_name_and_is_frozen():
    spec = ToolSpec(name="  echo  ", description="d", parameters={})
    assert spec.name == "echo"
    with pytest.raises(Exception):  # noqa: B017 —— frozen dataclass 的 FrozenInstanceError
        spec.name = "other"  # type: ignore[misc]


def test_tool_definition_carries_only_protocol_fields():
    """多余字段会让部分端点直接 400；模型能看到的就是这三样。"""
    spec = ToolSpec(
        name="echo", description="回显", parameters=ECHO_SCHEMA, permission=DANGEROUS,
    )
    definition = spec.to_tool_definition()
    assert set(definition) == {"type", "function"}
    assert set(definition["function"]) == {"name", "description", "parameters"}
    assert definition["type"] == "function"


# ── 注册表：装配与查询 ──────────────────────────

def test_duplicate_registration_is_rejected(registry):
    with pytest.raises(ToolError) as exc:
        registry.register(EchoTool())
    assert "重复" in str(exc.value)


def test_get_unknown_tool_lists_registered_names(registry):
    with pytest.raises(ToolError) as exc:
        registry.get("nope")
    assert "boom" in str(exc.value) and "echo" in str(exc.value)


def test_names_are_sorted_and_describe_is_complete(registry):
    assert registry.names() == ["boom", "echo"]
    rows = registry.describe()
    assert [row["name"] for row in rows] == ["boom", "echo"]
    assert set(rows[1]) == {
        "name", "description", "parameters", "permission",
        "idempotent", "timeout_s", "result_max_bytes",
    }


def test_tool_definitions_filtered_by_allowed(registry):
    assert [d["function"]["name"] for d in registry.tool_definitions()] == ["boom", "echo"]
    assert [d["function"]["name"] for d in registry.tool_definitions(["echo"])] == ["echo"]
    assert registry.tool_definitions([]) == []


# ── 白名单（§5.3 最小权限）──────────────────────

def test_not_in_whitelist_raises_before_running(registry, ctx):
    echo = registry.get("echo")
    with pytest.raises(ToolError) as exc:
        registry.invoke("echo", {"text": "hi"}, ctx, allowed=["boom"])
    assert NOT_ALLOWED in str(exc.value)
    assert echo.calls == []          # 关键：拦在执行之前，副作用为零


def test_none_whitelist_means_no_check(registry, ctx):
    assert registry.invoke("echo", {"text": "hi"}, ctx, allowed=None).ok is True


# ── 参数 schema ─────────────────────────────────

def test_missing_required_field_is_rejected_before_running(registry, ctx):
    echo = registry.get("echo")
    with pytest.raises(ToolError) as exc:
        registry.invoke("echo", {"tag": "a"}, ctx)
    assert BAD_ARGUMENTS in str(exc.value)
    assert "$" in str(exc.value)     # 违规路径必须原样带出来，否则只能靠猜
    assert echo.calls == []


def test_enum_violation_reports_path(registry, ctx):
    with pytest.raises(ToolError) as exc:
        registry.validate_args("echo", {"text": "hi", "tag": "z"})
    assert "$.tag" in str(exc.value)


def test_args_must_be_object(registry, ctx):
    with pytest.raises(ToolError) as exc:
        registry.invoke("echo", ["not", "a", "dict"], ctx)  # type: ignore[arg-type]
    assert BAD_ARGUMENTS in str(exc.value)


def test_empty_args_pass_when_nothing_required(registry, ctx):
    """``parameters`` 不声明 required 的工具，无参数调用是合法的。"""
    assert registry.invoke("boom", {}, ctx).ok is False   # 通过校验，然后在 run 里炸


# ── 调用与结算 ──────────────────────────────────

def test_invoke_returns_output_and_duration(registry, ctx):
    result = registry.invoke("echo", {"text": "hi"}, ctx)
    assert result.ok is True
    assert result.output == {"echo": "hi"}
    assert result.duration_ms >= 0
    assert result.truncated is False


def test_tool_exception_becomes_failed_result(registry, ctx):
    """工具自己失败必须是 ``ok=False`` 而不是异常：模型要看见原因才能换参数重试（D9）。"""
    result = registry.invoke("boom", {}, ctx)
    assert result.ok is False
    assert "ValueError" in result.error and "工具内部炸了" in result.error
    assert result.duration_ms >= 0


def test_tool_raised_tool_error_propagates(ctx):
    """调用方违规由工具抛出时不能被吞成 ok=False —— 那不是模型能修的问题。"""
    reg = ToolRegistry()
    reg.register(ContractViolationTool())
    with pytest.raises(ToolError):
        reg.invoke("strict", {}, ctx)


# ── 结果截断（D10）──────────────────────────────

def test_small_output_is_returned_as_is():
    payload = {"a": "b"}
    out, truncated = truncate_tool_output(payload, DEFAULT_RESULT_MAX_BYTES)
    assert out is payload and truncated is False


def test_oversized_output_keeps_head_tail_and_says_how_much_was_dropped():
    head_marker, tail_marker = "HEAD", "TAIL"
    text = head_marker + "x" * 5000 + tail_marker
    out, truncated = truncate_tool_output(text, 400)
    assert truncated is True
    assert out.startswith(head_marker)
    assert out.endswith(tail_marker)
    assert "已截断" in out and "字节" in out
    assert len(out.encode("utf-8")) <= 400


def test_truncation_never_splits_a_multibyte_character():
    out, truncated = truncate_tool_output("研" * 1000, 200)
    assert truncated is True
    assert len(out.encode("utf-8")) <= 200
    # 能在 utf-8 下重新编码成 str 就说明没有劈开字符（劈开会在解码时就抛）
    assert isinstance(out, str)


def test_dict_output_over_limit_gets_serialized_and_truncated():
    out, truncated = truncate_tool_output({"blob": "y" * 4000}, 300)
    assert truncated is True
    assert isinstance(out, str) and len(out.encode("utf-8")) <= 300


def test_invoke_applies_the_tools_own_limit(ctx):
    reg = ToolRegistry()

    class BigTool(Tool):
        def __init__(self) -> None:
            self.spec = ToolSpec(
                name="big", description="大输出", parameters={"type": "object"},
                result_max_bytes=64,
            )

        def run(self, args, ctx):  # noqa: ANN001
            return ToolResult(ok=True, output="z" * 2000)

    reg.register(BigTool())
    result = reg.invoke("big", {}, ctx)
    assert result.truncated is True
    assert len(str(result.output).encode("utf-8")) <= 64


# ── run_pipeline（D1：编排作为内核的一个能力）───

def _pipeline_tool(calls: list) -> RunPipelineTool:
    def runner(session, project_id, stage_ids=None, job_id=None):  # noqa: ANN001
        calls.append({"project_id": project_id, "stage_ids": stage_ids, "job_id": job_id})
        return list(range(1, len(stage_ids) + 1))

    return RunPipelineTool(runner=runner, stage_ids=["S1", "S2", "S3"])


def test_run_pipeline_delegates_with_injected_project_id():
    calls: list = []
    tool = _pipeline_tool(calls)
    result = tool.run({"stage_ids": ["S1", "S2"]}, ToolContext(session="S", project_id=7, job_id=3))
    assert result.ok is True
    assert result.output["requested"] == ["S1", "S2"]
    assert result.output["run_ids"] == [1, 2]
    assert result.output["stopped_early"] is False
    assert calls == [{"project_id": 7, "stage_ids": ["S1", "S2"], "job_id": 3}]


def test_run_pipeline_requires_project_id():
    """没有 project_id 说明装配错了，报错好过瞎跑（它也**不该**由模型提供）。"""
    result = _pipeline_tool([]).run({"stage_ids": ["S1"]}, ToolContext(project_id=None))
    assert result.ok is False
    assert "project_id" in result.error


def test_run_pipeline_reports_early_stop_instead_of_pretending():
    def runner(session, project_id, stage_ids=None, job_id=None):  # noqa: ANN001
        return [1]        # 请求 3 个，只跑出 1 条 —— 模拟中途暂停

    tool = RunPipelineTool(runner=runner, stage_ids=["S1", "S2", "S3"])
    result = tool.run({"stage_ids": ["S1", "S2", "S3"]}, ToolContext(project_id=1))
    assert result.output["stopped_early"] is True
    assert "暂停" in result.output["note"]


def test_run_pipeline_permission_is_execute_not_dangerous():
    """``run_pipeline`` 会真花钱、真写库，但它不删数据也不出网。

    定成 ``dangerous`` 会让每次调用都要批准，把审批疲劳变成常态 ——
    那才是「没人再看批准卡」的真正原因。风险要在**正确的那一档**上标。
    """
    permission = _pipeline_tool([]).spec.permission
    assert permission == EXECUTE
    assert permission != DANGEROUS


def test_run_pipeline_is_non_idempotent_with_long_timeout():
    """非幂等 → 调度层不得自动重试（跑两次就是两轮开销）；超时是给调度层的上界。"""
    spec = _pipeline_tool([]).spec
    assert spec.idempotent is False
    assert spec.timeout_s == 900.0


def test_run_pipeline_schema_requires_explicit_stage_ids():
    reg = ToolRegistry()
    reg.register(_pipeline_tool([]))
    with pytest.raises(ToolError) as exc:
        reg.validate_args("run_pipeline", {})
    assert "stage_ids" in str(exc.value)


def test_run_pipeline_schema_rejects_unknown_stage():
    reg = ToolRegistry()
    reg.register(_pipeline_tool([]))
    with pytest.raises(ToolError) as exc:
        reg.validate_args("run_pipeline", {"stage_ids": ["S9"]})
    assert "$.stage_ids[0]" in str(exc.value)


def test_run_pipeline_rejects_empty_stage_list():
    with pytest.raises(ValueError):
        RunPipelineTool(runner=lambda *a, **k: [], stage_ids=[])
