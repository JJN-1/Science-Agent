"""沙箱边界（US-406，对齐设计 §10.4 与决策 D6）。

这个文件回答两个问题：

1. **越界拦得住吗？** 路径穿越、绝对路径、符号链接逃逸 —— 三种都要在
   ``resolve`` 之后按真实路径判定，而不是靠字符串里有没有 ``..``。
2. **边界说清楚了吗？** ``ENFORCED`` 与 ``NOT_IMPLEMENTED`` 必须成对存在。
   只说能力的沙箱接口是一张宣传单：用户会据此把真实实验交给它跑。
   所以这里也断言 ``GET /api/sandbox`` 的数据源里**两张清单都有**。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agent_kernel.errors import KernelError, ToolError
from app.agent_kernel.sandbox import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_S,
    ENFORCED,
    NOT_IMPLEMENTED,
    SandboxPolicy,
    SandboxViolation,
    is_within,
    relative_to_project,
    resolve_within,
    sha256_bytes,
    sha256_file,
    truncated_output,
)


@pytest.fixture
def policy(tmp_path) -> SandboxPolicy:
    return SandboxPolicy.from_config(tmp_path / "workspace")


@pytest.fixture
def project_dir(policy, tmp_path) -> Path:
    return policy.ensure_project_dir(7)


# ── 越界：三种真实逃逸都要拦住 ──────────────────

def test_resolves_a_plain_relative_path_inside_the_project(policy, project_dir):
    target = resolve_within(policy, 7, "results/metrics.csv")
    assert target == (project_dir / "results" / "metrics.csv").resolve()


@pytest.mark.parametrize("raw", [
    "../secret.txt",
    "../../etc/passwd",
    "a/../../outside.txt",
    "..",
])
def test_rejects_parent_traversal(policy, raw):
    with pytest.raises(SandboxViolation) as exc:
        resolve_within(policy, 7, raw)
    assert exc.value.code == "AGENT-SANDBOX-001"


def test_rejects_absolute_paths_even_when_they_point_inside(tmp_path, policy):
    """**连「指向沙箱内部的绝对路径」也拒**。

    这一条看着过分，其实是刻意的：允许绝对路径意味着模型可以跨项目读写 ——
    它只要写 ``<workspace>/project-8/x`` 就绕过了「每个项目只能碰自己目录」这条
    唯一的隔离。相对路径 + 注入进来的 project_id 才是完整的坐标。
    """
    inside = policy.project_dir(7) / "ok.txt"
    with pytest.raises(SandboxViolation):
        resolve_within(policy, 7, str(inside))

    outside = tmp_path / "elsewhere.txt"
    with pytest.raises(SandboxViolation):
        resolve_within(policy, 7, str(outside))


def test_another_projects_directory_is_out_of_bounds(policy, project_dir):
    """跨项目读写必须被拦 —— 这是目录白名单存在的**全部**理由。"""
    other = policy.ensure_project_dir(8)
    (other / "theirs.txt").write_text("别人的数据", encoding="utf-8")
    with pytest.raises(SandboxViolation):
        resolve_within(policy, 7, "../project-8/theirs.txt")


def test_symlink_escape_is_caught_after_resolution(policy, project_dir, tmp_path):
    """符号链接逃逸：字符串里一个 ``..`` 都没有，只有 resolve 之后才看得出越界。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("机密", encoding="utf-8")
    link = project_dir / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):  # Windows 需要开发者模式或管理员
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(SandboxViolation):
        resolve_within(policy, 7, "link/secret.txt")


def test_empty_path_is_rejected(policy):
    for raw in (None, "", "   "):
        with pytest.raises(SandboxViolation):
            resolve_within(policy, 7, raw)


def test_must_exist_and_for_write_guards(policy, project_dir):
    with pytest.raises(SandboxViolation):
        resolve_within(policy, 7, "nope.txt", must_exist=True)

    # 父路径是个文件：写进去在磁盘上必然失败，但在闸门处就该说清「这是路径问题」
    (project_dir / "afile").write_text("x", encoding="utf-8")
    with pytest.raises(SandboxViolation):
        resolve_within(policy, 7, "afile/child.txt", for_write=True)


def test_violation_is_a_tool_error_so_it_lands_in_the_rejected_channel():
    """⚠️ 继承关系是有后果的，不是风格问题。

    ``ToolRegistry.invoke`` 只对 ``ToolError`` 原样上抛，其余异常一律兜成
    ``ok=False``；循环的 ``_invoke`` 也只把 ``ToolError`` 记成 ``rejected``。
    挂错父类的话，越界会被静默降级成「工具执行失败」——
    审计表里从此分不清「模型试图越狱」和「脚本自己报错」。
    """
    assert issubclass(SandboxViolation, ToolError)
    assert issubclass(SandboxViolation, KernelError)


# ── is_within 的两个 Windows 分支 ───────────────

def test_is_within_normalizes_case(tmp_path):
    """``C:\\Temp`` 与 ``c:\\temp`` 是同一个目录，比较时必须归一。"""
    parent = tmp_path / "Workspace"
    child = parent / "project-1" / "a.txt"
    assert is_within(child, parent)
    assert is_within(parent, parent)          # 含自身
    assert not is_within(parent.parent, parent)


def test_is_within_returns_false_across_drives():
    """跨盘符返回 ``False`` 而不是抛异常 —— 用 ``Path.is_relative_to`` 的话
    这里会 ValueError，而它恰好是「模型乱给一个绝对路径」最常见的形态。"""
    if os.name != "nt":
        pytest.skip("仅 Windows 存在盘符")
    assert not is_within(Path("D:/tmp/x"), Path("C:/tmp"))


# ── 相对路径回落 ────────────────────────────────

def test_relative_paths_stay_relative_and_use_forward_slashes(policy, project_dir):
    """落库的是相对路径：绝对路径会把本机用户名与盘符写进审计，
    换台机器回放时全部失效。"""
    nested = project_dir / "results" / "metrics.csv"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("a,b\n", encoding="utf-8")
    rel = relative_to_project(policy, 7, nested.resolve())
    assert rel == "results/metrics.csv"
    assert "\\" not in rel and ":" not in rel

    assert relative_to_project(policy, 7, project_dir.resolve()) == ""


def test_relative_to_project_refuses_outside_paths(policy):
    with pytest.raises(SandboxViolation):
        relative_to_project(policy, 7, Path(os.path.abspath(os.sep)))


# ── 策略构造 ────────────────────────────────────

def test_from_config_defaults_match_the_documented_ones(tmp_path):
    policy = SandboxPolicy.from_config(tmp_path)
    assert policy.timeout_s == DEFAULT_TIMEOUT_S
    assert policy.max_output_bytes == DEFAULT_MAX_OUTPUT_BYTES
    # 声明式：本版不做进程级网络隔离，这一位只用于展示与审计
    assert policy.network is False


def test_from_config_reads_overrides(tmp_path):
    policy = SandboxPolicy.from_config(
        tmp_path, {"timeout_s": 5, "max_output_bytes": 128, "network": True},
    )
    assert (policy.timeout_s, policy.max_output_bytes, policy.network) == (5.0, 128, True)


def test_policy_rejects_non_positive_limits(tmp_path):
    """一个 0 或负的上限等于「没有上限」还是「什么都做不了」？
    两种解读都说得通，所以它只能是装配错误。"""
    with pytest.raises(KernelError):
        SandboxPolicy(root=tmp_path, timeout_s=0)
    with pytest.raises(KernelError):
        SandboxPolicy(root=tmp_path, max_output_bytes=0)


def test_policy_is_frozen(tmp_path):
    policy = SandboxPolicy(root=tmp_path)
    with pytest.raises(Exception):  # noqa: B017
        policy.root = tmp_path / "other"  # type: ignore[misc]


def test_project_dir_is_not_created_on_read(policy):
    """读路径上调创建，会让「读一个不存在的项目目录」悄悄多出一个空目录，
    而调用方以为自己只是查了一下。"""
    assert not policy.project_dir(99).exists()
    policy.ensure_project_dir(99)
    assert policy.project_dir(99).is_dir()


# ── 截断与哈希 ──────────────────────────────────

def test_truncated_output_keeps_head_and_tail_and_says_how_much_was_dropped():
    raw = "H" * 5000 + "TAIL"
    text, truncated = truncated_output(raw, 512)
    assert truncated
    assert text.startswith("H")
    assert text.endswith("TAIL")
    assert "已截断" in text and "字节" in text
    assert len(text.encode("utf-8")) <= 512 + 64  # 上限 + 标记的余量


def test_short_output_is_returned_untouched():
    text, truncated = truncated_output("hello", 512)
    assert (text, truncated) == ("hello", False)


def test_sha256_helpers(tmp_path):
    target = tmp_path / "f.bin"
    target.write_bytes(b"abc")
    digest = sha256_file(target)
    assert digest == sha256_bytes(b"abc")


# ── 边界清单本身 ────────────────────────────────

def test_both_lists_are_present_and_disjoint():
    """能力与边界必须成对：只报能力是一张宣传单，只报边界是一片免责声明。"""
    assert ENFORCED and NOT_IMPLEMENTED
    assert not set(ENFORCED) & set(NOT_IMPLEMENTED)


def test_the_known_gaps_are_still_declared():
    """**这几条不是形式主义**。它们在骗人的方向上价值极高：
    含糊地声称「有沙箱」会让人把真实实验交给它跑。

    断言用「关键词在不在」而不是逐字比对 —— 措辞可以改，缺口不能悄悄消失。
    """
    blob = "".join(NOT_IMPLEMENTED)
    for keyword in ("受限令牌", "网络隔离", "快照", "配额", "进程树", "绝对路径"):
        assert keyword in blob, f"NOT_IMPLEMENTED 里没有交代「{keyword}」这条缺口"


def test_describe_returns_capabilities_and_gaps_together(tmp_path):
    described = SandboxPolicy.from_config(tmp_path).describe()
    assert described["enforced"] == list(ENFORCED)
    assert described["not_implemented"] == list(NOT_IMPLEMENTED)
    assert described["root"] == str(tmp_path)
