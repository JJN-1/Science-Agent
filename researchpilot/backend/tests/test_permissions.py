"""权限闸门（US-406，对齐设计 §10.1 与决策 D5）。

这份测试要钉住的是一条**否向**性质：``dangerous`` 档**永远**要批准。

其余三档都能用「批过就放行」来描述，只有 ``dangerous`` 的正确答案是
「不管批过多少次都还要批」—— 而它恰恰是最容易被一次「顺手优化」破坏的那条：
给判定加一个 ``granted`` 判断只要两行，加完之后**现有测试还会全绿**，
直到某天有人用一次批准换来了永久放行。

所以这里刻意把「记忆里明明有键，仍然要批」写成独立的一条。
"""

from __future__ import annotations

import json

import pytest

from app.agent_kernel.errors import KernelError
from app.agent_kernel.permissions import (
    APPROVAL_KIND_DANGEROUS,
    GRANT_PREFIX,
    ApprovalRequired,
    PendingCall,
    authorize,
    grant_key,
    scope_of,
)
from app.agent_kernel.tools.base import DANGEROUS, EXECUTE, READ, WRITE

# ── grant_key / scope_of ────────────────────────

def test_grant_key_is_tool_level_without_scope():
    assert grant_key("run_pipeline") == f"{GRANT_PREFIX}run_pipeline"
    assert grant_key("run_pipeline", None) == f"{GRANT_PREFIX}run_pipeline"
    assert grant_key("run_pipeline", "  ") == f"{GRANT_PREFIX}run_pipeline"


def test_grant_key_is_scoped_when_scope_given():
    """带作用域的键与工具级的键**不互相命中**。

    目录级的批准不该让同一工具在别的目录上也免批 —— 那等于「批了一个目录，
    放行了整台机器」。
    """
    tool_level = grant_key("run_shell")
    scoped = grant_key("run_shell", "workspace/a")
    assert tool_level != scoped
    assert scoped == f"{GRANT_PREFIX}run_shell:workspace/a"


def test_grant_key_rejects_empty_tool_name():
    with pytest.raises(ValueError):
        grant_key("   ")


def test_scope_of_reads_the_declared_argument_only():
    assert scope_of({"dir": "a/b"}, None) is None          # 工具没声明 scope_arg
    assert scope_of({}, "dir") is None                     # 参数没给
    assert scope_of({"dir": "  "}, "dir") is None          # 给了但为空
    assert scope_of({"dir": "a/b"}, "dir") == "a/b"
    # 非字符串照 ``str`` 归一：审批记忆的键必须是可比较的字符串，
    # 原样留下 123 这种值会让「同一个作用域」出现两种键。
    assert scope_of({"dir": 123}, "dir") == "123"


# ── authorize：四档处置 ─────────────────────────

def test_read_and_write_pass_through():
    for permission, tool in ((READ, "read_file"), (WRITE, "write_file")):
        decision = authorize(permission, tool_name=tool)
        assert decision.granted and not decision.needs_approval
        assert decision.grant_key is None, "直通档不该签发记忆键"


def test_execute_needs_approval_the_first_time():
    decision = authorize(EXECUTE, tool_name="run_pipeline")
    assert decision.needs_approval
    assert decision.grant_key == f"{GRANT_PREFIX}run_pipeline"
    assert "首次" in decision.reason


def test_execute_is_remembered_after_approval():
    key = grant_key("run_pipeline")
    decision = authorize(EXECUTE, tool_name="run_pipeline", granted=[key])
    assert decision.granted
    assert decision.grant_key == key


def test_execute_memory_does_not_cross_scopes():
    """批了 ``a/`` 不代表 ``b/`` 也放行。"""
    granted = [grant_key("run_shell", "a")]
    assert authorize(
        EXECUTE, tool_name="run_shell", granted=granted, scope="a",
    ).granted
    assert authorize(
        EXECUTE, tool_name="run_shell", granted=granted, scope="b",
    ).needs_approval
    # 工具级批准（无作用域）也不该被目录级的键命中 —— 反过来同样成立
    assert authorize(
        EXECUTE, tool_name="run_shell", granted=granted,
    ).needs_approval


def test_dangerous_still_needs_approval_when_a_grant_exists():
    """**本文件里最重要的一条**（§10.1：危险操作每次都要批，不可配置关闭）。

    注意这里给的是**恰好匹配**的键：如果判定改成了「有键就放行」，
    这条会从「需要批准」变成「直通」—— 而那正是要挡的那次改动。
    """
    key = grant_key("run_command")
    decision = authorize(DANGEROUS, tool_name="run_command", granted=[key])
    assert decision.needs_approval, "危险操作出现记忆就等于「一次批准换永久放行」"
    assert decision.grant_key is None, "dangerous 没有可签发的记忆，签了就会有人去用"
    assert "每次" in decision.reason


def test_unknown_permission_is_treated_as_the_strictest():
    """未知等级**拒绝而不是放行**：此刻正要执行一个能力不明的工具。"""
    decision = authorize("whatever", tool_name="mystery")
    assert decision.needs_approval
    assert "未知权限等级" in decision.reason


def test_decision_is_frozen():
    decision = authorize(READ, tool_name="read_file")
    with pytest.raises(Exception):  # noqa: B017
        decision.needs_approval = True  # type: ignore[misc]


def test_approval_kind_constant_is_the_one_the_table_uses():
    """``kind`` 是审批表与前端的分派键，写错一个字母会静默地弹错卡片。"""
    assert APPROVAL_KIND_DANGEROUS == "dangerous"


# ── PendingCall 的序列化 ────────────────────────

def _call(**overrides) -> PendingCall:
    base = dict(
        call_id="c1", tool_name="run_command", args={"argv": ["ls"]},
        permission=DANGEROUS, reason="危险操作每次都必须人工批准", grant_key=None,
    )
    base.update(overrides)
    return PendingCall(**base)


def test_pending_call_round_trips_through_json():
    """它要被写进 ``approvals.detail``（JSON 列），必须经得起一次往返。"""
    original = _call()
    revived = PendingCall.from_dict(json.loads(json.dumps(original.to_dict())))
    assert revived == original


def test_pending_call_defaults_to_needing_approval_when_the_key_is_absent():
    """老数据（没有 ``needs_approval`` 键）按**要批**处理。

    缺省成 ``False`` 会让一份记不清的记录变成一条放行 —— 这里宁可多问人一次。
    """
    raw = _call().to_dict()
    raw.pop("needs_approval")
    assert PendingCall.from_dict(raw).needs_approval is True


def test_pending_call_can_carry_a_pass_through_call():
    """同一轮里搭车的只读调用也在清单里，但 ``needs_approval=False``。"""
    item = _call(tool_name="read_file", permission=READ, needs_approval=False)
    assert item.to_dict()["needs_approval"] is False


# ── ApprovalRequired（挂起信号）─────────────────

def test_approval_required_is_not_a_kernel_error():
    """它**不是错误**。挂上 ``KernelError`` 就会被「一切内核错误都是失败」的
    兜底顺手变成 failed，而人还没机会看到审批单。"""
    assert not issubclass(ApprovalRequired, KernelError)


def test_approval_required_keeps_the_whole_round():
    """``pending`` 是要批的，``round`` 是整轮 —— 恢复时按后者重放。

    只带要批的那几条，assistant 那条消息里的 ``tool_calls`` 配对就永远差几条。
    """
    exc = ApprovalRequired((
        _call(call_id="c1"),
        _call(call_id="c2", tool_name="read_file", permission=READ, needs_approval=False),
    ))
    detail = exc.detail()
    assert [c["call_id"] for c in detail["pending"]] == ["c1"]
    assert [c["call_id"] for c in detail["round"]] == ["c1", "c2"]
    assert exc.reason


def test_approval_required_merges_reasons_without_repeating_them():
    exc = ApprovalRequired(tuple(_call(call_id=f"c{i}") for i in range(3)))
    assert exc.reason.count("危险操作每次都必须人工批准") == 1


def test_approval_required_rejects_a_round_with_nothing_to_approve():
    """一个「什么都不用批」的挂起是调用点的逻辑错误：它会静默地一条都不执行。"""
    with pytest.raises(ValueError):
        ApprovalRequired((_call(needs_approval=False),))
