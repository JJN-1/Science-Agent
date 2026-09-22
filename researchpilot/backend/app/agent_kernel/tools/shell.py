"""``run_command`` —— 沙箱内的命令执行（US-406）。

设计 §10.1 把「跑命令、跑实验」放在 ``execute`` 档，把「删除、覆盖、出网写、越出白名单」
放在 ``dangerous`` 档。一个任意命令执行工具**两样都能做**，所以它只可能定在 ``dangerous``：
定 ``execute`` 就等于宣称「这个工具不会删东西」，而那是它第一件能做的事。
``dangerous`` 的语义是**每次都要人工批准且没有记忆**——用户看到的不是「又一条审批」，
而是「这条命令我确实看过」。

四处必须写在明面上的取舍：

1. **``argv`` 数组而不是命令字符串**。``shell=True`` 会让模型给的字符串经过一层 shell：
   引号、``&&``、``$()`` 的语义全部由 shell 解释，工具这边事后无法还原「到底执行了什么」，
   而「事后无法还原」正是审计最怕的事。argv 形式下 ``argv[0]`` 就是可执行文件，
   参数边界是确定的。
2. **输出先落临时文件再截取**。``capture_output=True`` 会把一条刷屏命令的全部输出缓冲在
   内存里，再谈「输出上限」已经晚了 —— 上限必须作用在**读的时候**，不是收完之后。
3. **环境变量白名单**。子进程默认继承父进程的全部环境，其中包含后端凭据。
   一条 ``run_command(["set"])`` 就能把它们打印出来，而这与「不持有密钥」的约定直接冲突。
4. **超时只保证杀掉直接子进程**。它派生出的孙进程可能存活 —— 这是 Windows 上没有
   Job Object 的直接后果，写进 ``NOT_IMPLEMENTED`` 而不是假装没有。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

from app.agent_kernel.sandbox import truncation_budgets, truncation_marker
from app.agent_kernel.tools.base import (
    DANGEROUS,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from app.agent_kernel.tools.sandboxed import SandboxedTool
from app.ai.base import monotonic

#: 透传给子进程的环境变量白名单。
#: **刻意不含** ``*_API_KEY`` / ``*_TOKEN`` / ``*_SECRET`` 这类名字，也不含
#: ``PYTHONPATH``（它能把任意目录插进模块搜索路径，等于让一条命令替换掉本机的库）。
_ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "LANG", "LC_ALL", "LC_CTYPE", "PYTHONIOENCODING", "PYTHONUTF8",
    "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_ARCHITECTURE",
})


def child_env() -> dict[str, str]:
    """构造子进程的环境变量。

    按 ``upper()`` 比对而不是直接 ``os.environ["PATH"]``：Windows 上的实际键名是
    ``Path``，写死大写会让过滤结果**静默为空**，子进程于是连 ``python`` 都找不到 ——
    而那会被误诊成「这个工具没接好」。
    """
    return {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOWLIST}


class RunCommandTool(SandboxedTool):
    """在项目沙箱目录内执行一条命令。"""

    def __init__(self, *, policy) -> None:  # noqa: ANN001
        super().__init__(policy=policy)
        self.spec = ToolSpec(
            name="run_command",
            description=(
                "在项目沙箱目录内执行一条命令，**argv 数组形式**（不经 shell，"
                "因此没有管道与重定向）。cwd 只能在该项目的沙箱目录内。"
                "有超时与输出上限；退出码非 0 视为失败，输出会一并回传以便排错。"
                "这是需要人工批准的操作。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                        "description": (
                            "命令与参数分开给出，例如 [\"python\", \"scripts/run.py\", \"--seed\", \"0\"]。"
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": "相对项目沙箱目录的工作目录，缺省为项目沙箱根。",
                    },
                    "timeout_s": {
                        "type": "number",
                        "minimum": 1,
                        "description": "本次超时（秒），不得超过沙箱上限。",
                    },
                },
                "required": ["argv"],
            },
            # 见模块 docstring：任意命令执行工具只可能定在 dangerous
            permission=DANGEROUS,
            # 跑两次 = 两次真实副作用（可能是两次训练、两次写文件）
            idempotent=False,
            timeout_s=policy.timeout_s,
            result_max_bytes=self.result_budget(),
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        argv = [str(item) for item in args["argv"]]
        if not argv or not argv[0].strip():
            return ToolResult(ok=False, error="argv 不能为空，且第一项必须是非空的可执行文件名。")

        # ``must_exist=False`` 再自己判目录：项目沙箱目录可能还不存在（这是该项目
        # 第一条命令），而把它记成「文件不存在：.」会让模型以为沙箱坏了 ——
        # 真实原因只是「还没有任何产物」。下面按需创建，非法的子目录仍会被明确拒掉。
        cwd = self.resolve(ctx, args.get("cwd") or ".")
        self.policy.ensure_project_dir(self._project_id(ctx))
        if not cwd.is_dir():
            return ToolResult(
                ok=False,
                error=(
                    f"cwd 不存在或不是目录：{self.relative(ctx, cwd)}。"
                    "工作目录必须已经在项目沙箱内（先 write_file 建出来，或用项目根）。"
                ),
            )
        # 缺省取沙箱上限；模型给的值**只能调小**（调大等于取消这道闸）
        timeout = min(
            float(args.get("timeout_s") or self.policy.timeout_s),
            float(self.policy.timeout_s),
        )

        started = monotonic()
        with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
            try:
                proc = subprocess.run(
                    argv, cwd=str(cwd),
                    stdout=out_file, stderr=err_file,
                    env=child_env(),
                    timeout=timeout,
                    check=False,
                )
            except FileNotFoundError:
                return ToolResult(
                    ok=False,
                    error=(
                        f"找不到可执行文件：{argv[0]}。沙箱不透传父进程的密钥，"
                        "但会透传 PATH —— 若这是项目自带的解释器，请用可执行文件的完整路径。"
                    ),
                )
            except subprocess.TimeoutExpired:
                # 子进程已被 ``run`` 终止，但它**已经写进临时文件**的输出仍值得回传：
                # 超时的原因基本都在最后几行里（卡在哪一步、最后一条日志是什么）。
                #
                # ⚠️ 还留在子进程**自身缓冲区**里的输出拿不回来 —— 被 kill 的那一刻
                # 它就不存在了。这是超时的固有代价，不是这一层能补救的：
                # 要保住它，得让被执行的脚本自己 flush（或 ``-u``）。
                out = _capture(out_file, self.policy.max_output_bytes)
                err = _capture(err_file, self.policy.max_output_bytes)
                elapsed = monotonic() - started
                return ToolResult(ok=False, error=_failure_text(
                    f"执行超时（{timeout:g}s）已被终止，耗时 {elapsed:.1f}s",
                    out.text, err.text,
                ))
            except OSError as exc:
                return ToolResult(ok=False, error=f"命令无法启动：{exc}")

            duration_ms = int((monotonic() - started) * 1000)
            out = _capture(out_file, self.policy.max_output_bytes)
            err = _capture(err_file, self.policy.max_output_bytes)

        output: dict[str, Any] = {
            "argv": argv,
            "cwd": self.relative(ctx, cwd),
            "exit_code": proc.returncode,
            "stdout": out.text,
            "stderr": err.text,
            "stdout_bytes": out.size,
            "stderr_bytes": err.size,
            "truncated": out.truncated or err.truncated,
            "duration_ms": duration_ms,
            # 只在拿到全文时才给哈希。截断过的输出若也给一个哈希，那个哈希只覆盖
            # 前 64 KB 却看起来像代表整份输出 —— 事后拿它对不上，没人知道是文件变了
            # 还是当初算的就只是片段。
            "stdout_sha256": out.digest,
        }
        if proc.returncode != 0:
            # 退出码非 0 = 「工具跑了但没成」（D9 的 ``ok=False``）：模型据此换参数重试，
            # 连着三次同样的调用失败就把这一步交回给人。输出**必须**带上 ——
            # 否则模型只知道失败了，不知道失败在哪一行。
            return ToolResult(
                ok=False,
                output=output,
                error=_failure_text(
                    f"命令以退出码 {proc.returncode} 结束，耗时 {duration_ms}ms",
                    out.text, err.text,
                ),
            )
        return ToolResult(ok=True, output=output)


@dataclass(frozen=True)
class _Captured:
    """一路输出的读取结果。

    带 ``raw`` 是为了算**原始字节**的哈希：先 ``decode(errors="replace")`` 再
    ``encode`` 得到的是「解码后文本的字节」，与文件里真实写下的字节并不相同
    （替换符会被重新编码成别的字节）。用一个看起来对、实际对不上的哈希做产物核对，
    比不给哈希更糟。
    """

    text: str
    size: int
    truncated: bool
    raw: bytes | None = None

    @property
    def digest(self) -> str | None:
        return None if self.truncated or self.raw is None else hashlib.sha256(self.raw).hexdigest()


def _capture(handle: Any, limit: int) -> _Captured:
    """从临时文件里取输出：未超上限则全文，超限则头尾 + 截断说明。

    **头尾是分别 seek 出来的**，不是「先读满 limit 再交给截断函数」——
    那样读到的已经是恰好 limit 字节，截断函数会认为「没超，不用截」，
    于是标记永远不出现：结果看起来是一段完整的输出，实际尾部（错误栈）已经丢了。
    ``size`` 取文件大小而不是读到的长度：它才是「原输出有多少」。
    """
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    if size <= limit:
        handle.seek(0)
        raw = handle.read()
        return _Captured(raw.decode("utf-8", errors="replace"), size, False, raw)

    head_budget, tail_budget = truncation_budgets(limit)
    handle.seek(0)
    head = handle.read(head_budget)
    handle.seek(-tail_budget, os.SEEK_END)
    tail = handle.read(tail_budget)
    dropped = size - len(head) - len(tail)
    text = (
        head.decode("utf-8", errors="ignore")
        + truncation_marker(dropped)
        + tail.decode("utf-8", errors="ignore")
    )
    return _Captured(text, size, True)


def _failure_text(headline: str, stdout: str, stderr: str) -> str:
    """失败时给模型的一段话：结论 + 两路输出。

    失败路径上 ``CallOutcome.content`` 只回传 ``error``，``output`` 会被丢掉 ——
    所以这两段输出必须**写进 error** 才到得了模型眼前。
    """
    parts = [headline]
    if stdout.strip():
        parts.append(f"--- stdout ---\n{stdout}")
    if stderr.strip():
        parts.append(f"--- stderr ---\n{stderr}")
    if len(parts) == 1:
        parts.append("（命令没有任何输出）")
    return "\n".join(parts)
