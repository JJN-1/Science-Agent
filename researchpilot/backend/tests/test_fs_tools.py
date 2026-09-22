"""沙箱工具（US-406）：``read_file`` / ``write_file`` / ``list_dir`` / ``glob`` / ``grep`` / ``run_command``。

分两层断言：

- **普通路径**：工具确实干了它说的事（相对路径、SHA256、截断说明、条数上限）
- **边界路径**：越界一律抛 ``SandboxViolation`` 而不是回一个 ``ok=False``

第二层是重点。``ok=False`` 会被模型读成「这次没成，换个参数再试」——
而越界恰恰是「换个参数也不该过」。两者在审计表里也必须是两条不同的记录
（``failed`` vs ``rejected``）。

``run_command`` 的几条用 ``sys.executable`` 跑真实子进程：这是唯一能证明
「超时真的会终止」「环境变量真的被过滤」的办法 —— 打桩的话，断言的只是我们自己的假设。
"""

from __future__ import annotations

import sys

import pytest

from app.agent_kernel.sandbox import SandboxPolicy, SandboxViolation
from app.agent_kernel.tools.base import ToolContext
from app.agent_kernel.tools.factory import (
    SANDBOX_TOOL_NAMES,
    WITHHELD_TOOL_NAMES,
    build_registry,
)

PROJECT_ID = 7


@pytest.fixture
def policy(tmp_path) -> SandboxPolicy:
    # 超时放短：超时那条用例要真的等它
    return SandboxPolicy.from_config(tmp_path / "workspace", {"timeout_s": 8})


@pytest.fixture
def registry(policy):
    return build_registry(runner=lambda *a, **k: [], stage_ids=["S1"], policy=policy)


@pytest.fixture
def ctx() -> ToolContext:
    # ⚠️ project_id 由 ToolContext 注入，**不在 arguments 里**。
    # 模型若能自己指定项目号，目录白名单就形同虚设 ——
    # 它传别的项目号就能读写别人的产物。
    return ToolContext(project_id=PROJECT_ID)


def call(registry, name, args, ctx):
    """走注册表（而不是直接调工具）：白名单、schema、计时、截断都在那条路径上。"""
    return registry.invoke(name, args, ctx)


# ── 工具清单 ────────────────────────────────────

def test_registry_exposes_exactly_the_declared_tools(registry):
    assert set(SANDBOX_TOOL_NAMES) <= set(registry.names())
    assert set(WITHHELD_TOOL_NAMES) & set(registry.names()) == set()


def test_every_sandbox_tool_has_a_permission_out_of_the_four_levels(registry):
    from app.agent_kernel.tools.base import PERMISSIONS
    for name in SANDBOX_TOOL_NAMES:
        assert registry.get(name).spec.permission in PERMISSIONS, name


def test_run_command_is_the_only_dangerous_tool(registry):
    """任意命令执行只可能定在 ``dangerous``：定 ``execute`` 等于宣称
    「这个工具不会删东西」，而那是它第一件能做的事。"""
    from app.agent_kernel.tools.base import DANGEROUS
    dangerous = [s.name for s in registry.specs() if s.permission == DANGEROUS]
    assert dangerous == ["run_command"]


def test_file_writers_are_the_only_write_tools(registry):
    from app.agent_kernel.tools.base import WRITE
    assert [s.name for s in registry.specs() if s.permission == WRITE] == ["write_file"]


# ── 普通路径 ────────────────────────────────────

def test_write_then_read_round_trip(registry, ctx):
    written = call(registry, "write_file", {"path": "scripts/run.py", "content": "print('hi')\n"}, ctx)
    assert written.ok and written.output["created"] is True
    assert written.output["path"] == "scripts/run.py"     # 相对路径，不是绝对路径

    read = call(registry, "read_file", {"path": "scripts/run.py"}, ctx)
    assert read.ok
    assert read.output["content"] == "print('hi')\n"
    # 同一次写入前后取到的哈希必须一致：产物哈希的全部意义就是可核对
    assert read.output["sha256"] == written.output["sha256"]
    assert read.output["sha256"] == written.output["sha256"]


def test_write_file_creates_missing_parent_directories(registry, ctx):
    result = call(registry, "write_file", {"path": "a/b/c/d.txt", "content": "x"}, ctx)
    assert result.ok and result.output["created"]


def test_append_mode_does_not_overwrite(registry, ctx):
    call(registry, "write_file", {"path": "log.txt", "content": "1\n"}, ctx)
    result = call(registry, "write_file", {"path": "log.txt", "content": "2\n", "append": True}, ctx)
    assert result.ok and result.output["created"] is False and result.output["appended"]
    read = call(registry, "read_file", {"path": "log.txt"}, ctx)
    # 追加时给出的仍是**整个文件**的哈希，所以它必须等于对完整内容的哈希
    assert read.output["content"] == "1\n2\n"
    assert read.output["sha256"] == result.output["sha256"]


def test_overwrite_reports_created_false(registry, ctx):
    call(registry, "write_file", {"path": "x.txt", "content": "a"}, ctx)
    again = call(registry, "write_file", {"path": "x.txt", "content": "b"}, ctx)
    assert again.ok and again.output["created"] is False and not again.output["appended"]


def test_read_file_truncates_and_states_the_real_size(registry, ctx):
    """只读前 N 字节**并且**说明文件到底多大。

    少了后半句，模型会以为文件就这么长 —— 它的下一句推理就建立在一个错的前提上。
    """
    call(registry, "write_file", {"path": "big.txt", "content": "H" * 4000}, ctx)
    read = call(registry, "read_file", {"path": "big.txt", "max_bytes": 100}, ctx)
    assert read.ok and read.output["truncated"] is True
    assert read.output["size_bytes"] == 4000
    assert "文件共 4000 字节" in read.output["content"]


def test_read_file_rejects_binary_content(registry, ctx):
    """二进制当文本读出来是一大段替换符，而模型会拿它当内容继续推理。"""
    call(registry, "write_file", {"path": "x.bin", "content": "head\x00tail"}, ctx)
    result = call(registry, "read_file", {"path": "x.bin"}, ctx)
    assert not result.ok and "二进制" in result.error


def test_read_file_on_a_directory_says_which_tool_to_use(registry, ctx):
    call(registry, "write_file", {"path": "d/f.txt", "content": "x"}, ctx)
    result = call(registry, "read_file", {"path": "d"}, ctx)
    assert not result.ok and "list_dir" in result.error


def test_list_dir_is_not_recursive_and_reports_types(registry, ctx):
    call(registry, "write_file", {"path": "d/f.txt", "content": "x"}, ctx)
    result = call(registry, "list_dir", {}, ctx)
    assert result.ok
    assert [(e["name"], e["type"]) for e in result.output["entries"]] == [("d", "dir")]
    inner = call(registry, "list_dir", {"path": "d"}, ctx)
    assert [e["name"] for e in inner.output["entries"]] == ["f.txt"]
    assert inner.output["entries"][0]["size_bytes"] == 1


def test_list_dir_on_a_missing_directory_is_not_an_error(registry, ctx):
    """「这个目录还不存在」是有效信息（还没有任何产物），不是失败 ——
    报成失败会让模型去重试一个它无法解决的问题。"""
    result = call(registry, "list_dir", {"path": "nope"}, ctx)
    assert result.ok and result.output["exists"] is False


def test_glob_matches_recursively_and_only_files(registry, ctx):
    call(registry, "write_file", {"path": "scripts/a.py", "content": "x"}, ctx)
    call(registry, "write_file", {"path": "data/b.csv", "content": "x"}, ctx)
    result = call(registry, "glob", {"pattern": "**/*.py"}, ctx)
    assert result.ok and result.output["matches"] == ["scripts/a.py"]
    assert result.output["total"] == 1


def test_glob_respects_the_limit(registry, ctx):
    for i in range(5):
        call(registry, "write_file", {"path": f"f{i}.txt", "content": "x"}, ctx)
    result = call(registry, "glob", {"pattern": "*.txt", "limit": 2}, ctx)
    assert len(result.output["matches"]) == 2
    assert result.output["total"] == 5 and result.output["truncated"] is True


def test_grep_reports_file_and_line_number(registry, ctx):
    call(registry, "write_file", {"path": "s/a.py", "content": "import os\nVALUE = 42\n"}, ctx)
    result = call(registry, "grep", {"pattern": r"VALUE\s*=", "glob": "*.py"}, ctx)
    assert result.ok
    assert result.output["hits"][0]["path"] == "s/a.py"
    assert result.output["hits"][0]["line"] == 2


def test_grep_reports_an_invalid_regex_as_a_retryable_failure(registry, ctx):
    """正则不合法是**模型能自己修**的问题：带回去编译器原话，它下一轮就会改。"""
    result = call(registry, "grep", {"pattern": "([unclosed"}, ctx)
    assert not result.ok and "正则" in result.error


def test_grep_stops_at_max_hits(registry, ctx):
    call(registry, "write_file", {"path": "many.txt", "content": "hit\n" * 20}, ctx)
    result = call(registry, "grep", {"pattern": "hit", "max_hits": 3}, ctx)
    assert len(result.output["hits"]) == 3 and result.output["truncated"] is True


# ── 越界：必须是 SandboxViolation，不是 ok=False ──

@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": "../secret.txt"}),
    ("write_file", {"path": "../evil.txt", "content": "x"}),
    ("list_dir", {"path": "../.."}),
    ("glob", {"pattern": "../*"}),
    ("grep", {"pattern": "x", "path": "../.."}),
    ("run_command", {"argv": ["echo", "x"], "cwd": "../.."}),
])
def test_path_escape_is_rejected_not_failed(registry, ctx, name, args):
    """``rejected`` 而不是 ``failed``。

    做成 ``ok=False`` 会让模型以为「换个参数就能过」，然后反复撞墙；
    审计表里也从此分不清「模型试图越界」与「工具自己报错」。
    """
    with pytest.raises(SandboxViolation) as exc:
        call(registry, name, args, ctx)
    assert exc.value.code == "AGENT-SANDBOX-001"


def test_absolute_path_is_rejected(registry, ctx, tmp_path):
    with pytest.raises(SandboxViolation):
        call(registry, "read_file", {"path": str(tmp_path / "outside.txt")}, ctx)


def test_another_project_is_out_of_bounds(registry, ctx, policy):
    other = policy.ensure_project_dir(PROJECT_ID + 1)
    (other / "theirs.txt").write_text("别人的数据", encoding="utf-8")
    with pytest.raises(SandboxViolation):
        call(registry, "read_file", {"path": f"../project-{PROJECT_ID + 1}/theirs.txt"}, ctx)


def test_tools_refuse_to_run_without_a_project_id(registry):
    """缺 ``project_id`` 时**抛错而不是回退到某个默认项目**。

    回退会让「没配好」表现为「写进了另一个项目」，而那是最难查的一类串数据。
    """
    with pytest.raises(SandboxViolation) as exc:
        call(registry, "write_file", {"path": "a.txt", "content": "x"}, ToolContext())
    assert "project_id" in str(exc.value)


# ── run_command ─────────────────────────────────

def _py(code: str, *rest: str) -> list[str]:
    return [sys.executable, "-c", code, *rest]


def test_run_command_reports_exit_code_and_streams(registry, ctx):
    result = call(registry, "run_command", {
        "argv": _py("import sys; print('out'); print('err', file=sys.stderr)"),
    }, ctx)
    assert result.ok
    assert result.output["exit_code"] == 0
    assert result.output["stdout"].strip() == "out"
    assert result.output["stderr"].strip() == "err"
    assert result.output["cwd"] == ""
    # 未超上限 → 给出原始字节的哈希
    assert result.output["stdout_sha256"]


def test_run_command_non_zero_exit_is_a_failure_that_carries_the_output(registry, ctx):
    """退出码非 0 = 「工具跑了但没成」（D9 的 ``ok=False``）。

    输出**必须**跟着走：失败路径上 ``CallOutcome.content`` 只回传 ``error``，
    不带的话模型只知道失败了，不知道失败在哪一行。
    """
    result = call(registry, "run_command", {
        "argv": _py("import sys; print('partial'); sys.exit(3)"),
    }, ctx)
    assert not result.ok
    assert "退出码 3" in result.error
    assert "partial" in result.error


def test_run_command_timeout_kills_and_reports_partial_output(registry, ctx):
    """超时前的输出要跟着回传。

    ``flush=True`` 不能省：子进程的 stdout 是文件不是终端，``print`` 默认进 8 KB
    缓冲区。**还留在子进程缓冲区里的输出，在被 kill 的那一刻就已经丢了** ——
    那是超时的固有代价（没有 flush 谁也无从拿到它），不是这一层能补救的。
    """
    result = call(registry, "run_command", {
        "argv": _py("import time; print('before', flush=True); time.sleep(30)"),
        "timeout_s": 1,
    }, ctx)
    assert not result.ok
    assert "超时" in result.error
    assert "before" in result.error, "超时前的输出正是排查超时原因最需要的东西"


def test_run_command_cannot_raise_the_sandbox_timeout(registry, ctx):
    """模型给的值只能把闸门调**紧**。能调松的话，这道闸就等于没有。"""
    result = call(registry, "run_command", {
        "argv": _py("import time; print('x'); time.sleep(30)"),
        "timeout_s": 9999,
    }, ctx)
    assert not result.ok and "超时" in result.error


def test_run_command_cwd_is_locked_to_the_project_sandbox(registry, ctx):
    result = call(registry, "run_command", {
        "argv": _py("import os; print(os.getcwd())"),
    }, ctx)
    assert result.ok
    # 反斜杠在 Windows 上会以 ``\r\n`` 结尾，比较前先归一
    reported = result.output["stdout"].strip().replace("\\", "/").lower()
    assert f"/project-{PROJECT_ID}" in reported


def test_run_command_does_not_inherit_credentials(registry, ctx, monkeypatch):
    """子进程默认继承父进程全部环境，其中包含后端凭据 ——
    一条 ``run_command(["set"])`` 就能把它们打印出来。"""
    monkeypatch.setenv("RESEARCHPILOT_TEST_API_KEY", "sk-must-not-leak")
    result = call(registry, "run_command", {
        "argv": _py(
            "import os; print(sorted(k for k in os.environ if 'API_KEY' in k.upper()))"
        ),
    }, ctx)
    assert result.ok
    assert result.output["stdout"].strip() == "[]"


def test_run_command_does_not_go_through_a_shell(registry, ctx):
    """``argv`` 形式下 ``&&`` 只是一个普通参数，不会被 shell 解释成第二次执行。"""
    result = call(registry, "run_command", {
        "argv": _py("import sys; print(sys.argv[1])", "a && b"),
    }, ctx)
    assert result.ok and result.output["stdout"].strip() == "a && b"


def test_run_command_reports_a_missing_executable_as_a_retryable_failure(registry, ctx):
    result = call(registry, "run_command", {"argv": ["definitely-not-a-real-binary-xyz"]}, ctx)
    assert not result.ok and "找不到可执行文件" in result.error


def test_run_command_rejects_empty_argv(registry, ctx):
    result = call(registry, "run_command", {"argv": [""]}, ctx)
    assert not result.ok


def test_run_command_truncates_huge_output_without_buffering_it_all(registry, ctx):
    """上限作用在**读的时候**。先收全再截断的话，一条刷屏命令会先撑爆内存。"""
    result = call(registry, "run_command", {
        "argv": _py("import sys; sys.stdout.write('x' * 500000)"),
    }, ctx)
    assert result.ok
    assert result.output["stdout_bytes"] == 500000
    assert result.output["truncated"] is True
    assert "已截断" in result.output["stdout"]
    # 超限时**不给哈希**：一个只覆盖前 64 KB 的哈希看起来像代表整份输出，
    # 事后拿它对不上，没人知道是文件变了还是当初算的就只是片段。
    assert result.output["stdout_sha256"] is None
