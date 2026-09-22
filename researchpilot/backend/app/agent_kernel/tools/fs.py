"""沙箱内的文件工具（US-406）：``read_file`` / ``write_file`` / ``list_dir`` / ``glob`` / ``grep``。

五个工具都只做一件事：把「模型想动哪个文件」翻译成「沙箱内的哪个路径」。所有路径
一律经 ``resolve_within``（先 ``resolve`` 再比对边界），任何越界都抛
``SandboxViolation`` → 在循环里记成 ``rejected``（调用方违规，副作用为零）。

三处刻意的取舍：

1. **落库与回传一律用相对路径**。绝对路径会把本机用户名与盘符写进审计记录，
   换台机器回放时那些路径全部失效 —— 而相对路径仍指向同一个产物。
2. **``read_file`` 只读前 N 字节，不把整个文件读进内存**。实验产物可能是几百 MB 的
   数据文件；先读全再截断，等于让一次「看一眼」变成一次 OOM。哈希走流式，
   与读取量无关。
3. **二进制文件直接拒**（按 NUL 字节判断）。把二进制当文本 ``errors="replace"`` 读出来
   是一大段替换符，模型会拿它当内容继续推理 —— 而它其实什么都没读到。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.agent_kernel.sandbox import SandboxViolation, is_within, sha256_file
from app.agent_kernel.tools.base import (
    READ,
    WRITE,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from app.agent_kernel.tools.sandboxed import SandboxedTool

#: 单个文件在 ``grep`` 里最多扫多少字节。超过就跳过：一次全库文本搜索不该被
#: 一个 2 GB 的日志文件拖住，而那份日志本来也不该用 grep 读。
GREP_MAX_FILE_BYTES = 1 << 20

#: ``list_dir`` / ``glob`` / ``grep`` 的默认条数上限。上限存在的意义不是省资源，
#: 而是让结果**能一眼看完**：几千条目录项回填给模型，只会把上下文里的其它内容挤掉。
LIST_DEFAULT_LIMIT = 500
GLOB_DEFAULT_LIMIT = 200
GREP_DEFAULT_MAX_HITS = 100

#: 单条 grep 命中回传的文本长度上限
GREP_TEXT_LIMIT = 300


class ReadFileTool(SandboxedTool):
    """读一个文本文件。"""

    def __init__(self, *, policy) -> None:  # noqa: ANN001 - 见 SandboxedTool
        super().__init__(policy=policy)
        self.spec = ToolSpec(
            name="read_file",
            description=(
                "读取项目沙箱内的一个文本文件。返回相对路径、文件总字节数、SHA256 与内容。"
                "文件超过读取上限时只返回开头部分，并明确说明文件总大小。"
                "二进制文件会被拒绝。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "相对项目沙箱目录的路径，例如 results/metrics.csv",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "本次最多读取多少字节。不得超过沙箱上限，缺省即沙箱上限。",
                    },
                },
                "required": ["path"],
            },
            permission=READ,
            idempotent=True,
            result_max_bytes=self.result_budget(),
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = self.resolve(ctx, args["path"], must_exist=True)
        if path.is_dir():
            return ToolResult(
                ok=False,
                error=f"{self.relative(ctx, path)} 是目录不是文件；列目录请用 list_dir。",
            )

        size = path.stat().st_size
        want = int(args.get("max_bytes") or self.spec.result_max_bytes)
        # 模型不能抬高上限：它要是能指定 1 GB，这一层就等于没有上限
        limit = max(1, min(want, self.policy.max_output_bytes))

        with path.open("rb") as handle:
            head = handle.read(limit)
        if b"\x00" in head:
            return ToolResult(
                ok=False,
                error=(
                    f"{self.relative(ctx, path)} 看起来是二进制文件（含 NUL 字节），"
                    "未按文本读取。"
                ),
            )

        text = head.decode("utf-8", errors="replace")
        truncated = size > len(head)
        if truncated:
            text += f"\n…[文件共 {size} 字节，本次只读到前 {len(head)} 字节]…"
        return ToolResult(ok=True, output={
            "path": self.relative(ctx, path),
            "size_bytes": size,
            "sha256": sha256_file(path),
            "truncated": truncated,
            "content": text,
        })


class WriteFileTool(SandboxedTool):
    """在沙箱内写一个文件。"""

    def __init__(self, *, policy) -> None:  # noqa: ANN001
        super().__init__(policy=policy)
        self.spec = ToolSpec(
            name="write_file",
            description=(
                "在项目沙箱内写入一个文件（缺省覆盖，可选追加）。"
                "父目录不存在时会自动创建。返回相对路径、写入字节数与整个文件的 SHA256。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "相对项目沙箱目录的路径，例如 scripts/run.py",
                    },
                    "content": {"type": "string", "description": "要写入的文本内容。"},
                    "append": {
                        "type": "boolean",
                        "description": "true 表示追加到文件末尾而不是覆盖。缺省 false。",
                    },
                },
                "required": ["path", "content"],
            },
            # §10.1：``write`` 是「写草稿」，直通；真正的兜底是第 7 步的写前快照
            permission=WRITE,
            idempotent=True,
            result_max_bytes=self.result_budget(),
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = self.resolve(ctx, args["path"], for_write=True)
        if path.is_dir():
            return ToolResult(
                ok=False, error=f"{self.relative(ctx, path)} 是目录，不能写文件。",
            )
        # 项目目录按需创建：写一个项目的第一个产物时它还不存在。
        # 放在 resolve 之后（边界已经验过），否则等于允许「写到任意目录再报错」。
        self.policy.ensure_project_dir(self._project_id(ctx))
        path.parent.mkdir(parents=True, exist_ok=True)

        append = bool(args.get("append"))
        payload = str(args["content"]).encode("utf-8")
        created = not path.exists()
        with path.open("ab" if append else "wb") as handle:
            handle.write(payload)
        return ToolResult(ok=True, output={
            "path": self.relative(ctx, path),
            "bytes_written": len(payload),
            "created": created,
            "appended": append,
            # 追加时这里会把整个文件重新哈希一遍。为「整个文件的产物哈希」付这个代价
            # 是值的：只报本次写入片段的哈希，事后就无法用它核对文件本身有没有被改过。
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        })


class ListDirTool(SandboxedTool):
    """列一个目录。"""

    def __init__(self, *, policy) -> None:  # noqa: ANN001
        super().__init__(policy=policy)
        self.spec = ToolSpec(
            name="list_dir",
            description=(
                "列出项目沙箱内某个目录的直接子项（不递归）。"
                "缺省列出项目沙箱根目录。目录不存在时返回空列表而不是报错。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对项目沙箱目录的路径，缺省为项目根（用 . 表示）。",
                    },
                },
                "required": [],
            },
            permission=READ,
            idempotent=True,
            result_max_bytes=self.result_budget(),
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = self.resolve(ctx, args.get("path") or ".")
        if not path.exists():
            # 「这个目录还不存在」本身是有效信息（说明还没有任何产物），
            # 报成失败会让模型去重试一个它无法解决的问题。
            return ToolResult(ok=True, output={
                "path": self.relative(ctx, path), "exists": False, "entries": [],
            })
        if not path.is_dir():
            return ToolResult(
                ok=False, error=f"{self.relative(ctx, path)} 是文件不是目录；读它请用 read_file。",
            )

        items: list[dict[str, Any]] = []
        for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            try:
                is_dir = entry.is_dir()
                size = None if is_dir else entry.stat().st_size
            except OSError:
                # 断链的符号链接、无权限的项：跳过而不是让整次列举失败。
                # 「有几项读不到」不该表现为「这个目录列不出来」。
                continue
            items.append({
                "name": entry.name,
                "type": "dir" if is_dir else "file",
                "size_bytes": size,
            })
        truncated = len(items) > LIST_DEFAULT_LIMIT
        return ToolResult(ok=True, output={
            "path": self.relative(ctx, path),
            "exists": True,
            "entries": items[:LIST_DEFAULT_LIMIT],
            "total": len(items),
            "truncated": truncated,
        })


class GlobTool(SandboxedTool):
    """按模式匹配沙箱内的文件。"""

    def __init__(self, *, policy) -> None:  # noqa: ANN001
        super().__init__(policy=policy)
        self.spec = ToolSpec(
            name="glob",
            description=(
                "在项目沙箱内按 glob 模式查找文件（递归用 **，例如 results/**/*.csv）。"
                "只返回文件，不返回目录；返回相对路径。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "description": "相对项目沙箱目录的模式，例如 **/*.csv 或 scripts/*.py",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": f"最多返回多少条，缺省 {GLOB_DEFAULT_LIMIT}。",
                    },
                },
                "required": ["pattern"],
            },
            permission=READ,
            idempotent=True,
            result_max_bytes=self.result_budget(),
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args["pattern"]).strip()
        base = self.project_dir(ctx)
        # ``Path.glob`` 对绝对模式直接抛 ``ValueError``、对 ``..`` 会走出目录。
        # 在这里先明确拒掉，好过让越界表现为「匹配到 0 个文件」——后者会让模型
        # 以为文件不存在，然后换个猜测继续试。
        if (
            pattern.startswith("/")
            or os.path.isabs(pattern)
            or ".." in pattern.replace("\\", "/").split("/")
        ):
            raise SandboxViolation(
                f"glob 模式必须是沙箱内的相对路径（不接受绝对路径或 ..）：{pattern}"
            )

        limit = max(1, int(args.get("limit") or GLOB_DEFAULT_LIMIT))
        if not base.exists():
            return ToolResult(ok=True, output={
                "pattern": pattern, "matches": [], "total": 0, "truncated": False,
            })

        found: list[str] = []
        for candidate in base.glob(pattern):
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not candidate.is_file():
                continue
            # ``**`` 在 3.13 起默认不跟随符号链接，但显式 glob 出来的链接仍可能是目录外
            # 的目标；一律按解析后的真实路径再判一次边界。
            if not is_within(resolved, base):
                continue
            found.append(self.relative(ctx, resolved))
        found.sort()
        truncated = len(found) > limit
        return ToolResult(ok=True, output={
            "pattern": pattern,
            "matches": found[:limit],
            "total": len(found),
            "truncated": truncated,
        })


class GrepTool(SandboxedTool):
    """在沙箱内的文本文件里按正则搜索。"""

    def __init__(self, *, policy) -> None:  # noqa: ANN001
        super().__init__(policy=policy)
        self.spec = ToolSpec(
            name="grep",
            description=(
                "在项目沙箱内的文本文件里按正则搜索，返回命中的文件、行号与该行内容。"
                "可用 glob 参数限定文件名，例如 *.py。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "minLength": 1, "description": "正则表达式。"},
                    "path": {
                        "type": "string",
                        "description": "搜索起点（相对项目沙箱目录），缺省为项目根。",
                    },
                    "glob": {
                        "type": "string",
                        "description": "只搜索文件名匹配该 glob 的文件，例如 *.py。",
                    },
                    "max_hits": {
                        "type": "integer",
                        "minimum": 1,
                        "description": f"最多返回多少条命中，缺省 {GREP_DEFAULT_MAX_HITS}。",
                    },
                },
                "required": ["pattern"],
            },
            permission=READ,
            idempotent=True,
            result_max_bytes=self.result_budget(),
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            regex = re.compile(str(args["pattern"]))
        except re.error as exc:
            # 正则不合法是**模型能自己修**的问题：把编译器原话带回去，它下一轮就会改
            return ToolResult(ok=False, error=f"正则表达式不合法：{exc}")

        root = self.resolve(ctx, args.get("path") or ".", must_exist=True)
        name_glob = str(args.get("glob") or "").strip()
        max_hits = max(1, int(args.get("max_hits") or GREP_DEFAULT_MAX_HITS))

        hits: list[dict[str, Any]] = []
        scanned = 0
        skipped_big = 0
        for file_path in self._walk(ctx, root):
            if name_glob and not file_path.match(name_glob):
                continue
            try:
                if file_path.stat().st_size > GREP_MAX_FILE_BYTES:
                    skipped_big += 1
                    continue
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            rel = self.relative(ctx, file_path)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if not regex.search(line):
                    continue
                hits.append({
                    "path": rel,
                    "line": lineno,
                    "text": line.strip()[:GREP_TEXT_LIMIT],
                })
                if len(hits) >= max_hits:
                    break
            if len(hits) >= max_hits:
                break
        output: dict[str, Any] = {
            "pattern": str(args["pattern"]),
            "hits": hits,
            "files_scanned": scanned,
            "truncated": len(hits) >= max_hits,
        }
        if skipped_big:
            # 「扫了几个」与「跳过了几个」都要说：只说命中数，模型无从判断
            # 「没搜到」是因为真的没有，还是因为那份大文件根本没被打开。
            output["skipped_too_large"] = skipped_big
        return ToolResult(ok=True, output=output)

    def _walk(self, ctx: ToolContext, root: Path):
        """产出 ``root`` 下的文件。符号链接不跟随，越界的项直接剔除。"""
        base = self.project_dir(ctx)
        if root.is_file():
            yield root
            return
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                candidate = Path(dirpath) / name
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if not is_within(resolved, base):
                    continue
                yield resolved
