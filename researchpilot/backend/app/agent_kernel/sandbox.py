"""最小可验证沙箱（US-406，对齐设计 §10.4 与决策 D6）。

设计 §10.4 自己承认「Windows 上没有轻量容器，沙箱是真实技术难点」，并给出备选方案
（受限令牌 / 独立用户账户 / WSL2）。D6 的裁定是：**第一版只做可验证的最小面**，并把
「不做什么」写成常量 ``NOT_IMPLEMENTED`` 暴露出去。

这不是自我设限，而是**责任边界**：含糊地声称「有沙箱」比明确说「这几条还没做」危险得多 ——
用户会据此把真实实验交给它跑。`GET /api/sandbox` 会把这两张清单一起返回，让边界可核对而不是
只能靠读源码判断。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent_kernel.errors import KernelError, ToolError

#: 子进程默认超时（秒）。与 ``ToolSpec.timeout_s`` 是两回事：后者是「这个工具多久算超时」，
#: 这里是「沙箱允许子进程活多久」—— 前者可以随工具调，后者是硬上限。
DEFAULT_TIMEOUT_S = 30.0

#: 子进程输出上限。超出即截断并在结果里写明丢了多少 —— 与 D10 同一条原则：
#: 静默截断等于对模型撒谎，它会在「输出就这么长」的前提下继续推理。
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024

#: ``workspace`` 下的项目子目录前缀。
PROJECT_DIR_PREFIX = "project-"

#: 第一版**明确不做**的事。逐条写出来，供 ``GET /api/sandbox`` 与文档核对。
#: 顺序按「离危险动作的距离」排：越靠前越是「有人会以为已经具备」的能力。
NOT_IMPLEMENTED: tuple[str, ...] = (
    "受限令牌 / Job Object / 只读根文件系统（设计 §10.4 的完整方案，属 S9 交付）",
    "进程级网络隔离：本版通过「不向任何 Agent 提供网络工具」缩小暴露面，"
    "但**不阻断**子进程自己出网（例如 `run_command` 里的 curl）",
    "写前自动快照与一键回滚：落点在 ``kernel_checkpoints``（第 7 步）",
    "磁盘/内存配额：只有超时与输出上限两道闸，没有 cgroup 那样的硬配额",
    "进程树终止：超时只保证杀掉直接子进程，它再派生出的孙进程可能存活",
    "子进程的文件系统隔离：``run_command`` 的 argv 里可以出现绝对路径，白名单只约束"
    "**我们自己解析**的路径（cwd 与文件工具的入参）。真正拦住子进程越界读写需要"
    "受限令牌或容器 —— 这也正是它被定在 ``dangerous``、每次都要人工批准的原因",
)

#: 第一版**确实做到**的事。与上面那张表成对，避免只说边界不说能力。
ENFORCED: tuple[str, ...] = (
    "目录白名单：文件工具（read_file / write_file / list_dir / glob / grep）的读写"
    "一律解析到 ``workspace/project-<id>/`` 之内，越界即拒（按 ``rejected`` 记账，副作用为零）",
    "路径穿越防护：``..``、绝对路径、符号链接逃逸都会在 resolve 之后被拦下",
    "固定 cwd：``run_command`` 的启动工作目录锁在该项目的沙箱目录，不继承启动进程的 cwd"
    "（命令仍可用绝对路径自行跳出去，见 not_implemented）",
    "超时终止 + 输出上限：命令输出走临时文件再截取头尾，不会先撑爆内存",
    "环境变量白名单：子进程拿不到父进程的密钥（只透传 PATH/TEMP 等运行必需项）",
    "产物 SHA256：``write_file`` 流式哈希落盘后的整个文件；命令输出在**未超上限**时"
    "按原始字节整体哈希，超限时只给头尾并明确不给哈希（避免给出一个只覆盖片段的假哈希）",
    "危险操作强制事前审批：``run_command`` 每次都要批，不可配置关闭",
)


class SandboxViolation(ToolError):
    """越出沙箱边界。

    与「工具跑了但失败」不同，越界是**调用方违规**，应当在副作用发生**之前**被拦下。
    做成 ``ToolResult(ok=False)`` 会让模型以为「换个参数就能过」，然后反复撞墙。

    ⚠️ **它继承 ``ToolError`` 而不是 ``KernelError``**，这一点是有实际后果的：
    ``ToolRegistry.invoke`` 只对 ``ToolError`` 原样上抛，其余异常一律兜成
    ``ok=False``；而循环的 ``_invoke`` 也只把 ``ToolError`` 记成 ``rejected``。
    挂在 ``KernelError`` 下面的话，越界会被静默降级成一条「工具执行失败」——
    审计表里从此分不清「模型试图越狱」和「脚本自己报错」。
    子码用 ``AGENT-SANDBOX-001`` 与工具契约的三个子码并列。
    """

    code = "AGENT-SANDBOX-001"


@dataclass(frozen=True)
class SandboxPolicy:
    """沙箱边界。**冻结**：它在装配期确定，不该在运行中被工具改写。

    ``root`` 是 ``workspace`` 根，不是项目目录本身 —— 项目目录由 ``project_dir()`` 派生。
    这样策略只需构造一次，而不同项目的产物天然隔离（拼在一起的 ``workspace/`` 会让
    A 项目的 ``results.csv`` 被 B 项目的实验覆盖，且事后查不出是谁写的）。
    """

    root: Path
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    #: 声明式：本版**不做**进程级网络隔离，这一位只用于展示与审计（见 NOT_IMPLEMENTED）
    network: bool = False

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise KernelError(f"沙箱超时必须为正，实际 {self.timeout_s}")
        if self.max_output_bytes <= 0:
            raise KernelError(f"沙箱输出上限必须为正，实际 {self.max_output_bytes}")

    @classmethod
    def from_config(cls, root: Path | str, cfg: dict[str, Any] | None = None) -> SandboxPolicy:
        """从 ``config.yaml`` 的 ``sandbox:`` 段构造。缺省值即 D6 的第一版边界。"""
        cfg = cfg or {}
        return cls(
            root=Path(root),
            timeout_s=float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S)),
            max_output_bytes=int(cfg.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)),
            network=bool(cfg.get("network", False)),
        )

    def project_dir(self, project_id: int) -> Path:
        """该项目的沙箱目录。**不创建**——创建发生在真正要写入的那一刻（``ensure_project_dir``）。

        读路径上调创建会让「读一个不存在的项目目录」悄悄多出一个空目录，
        而调用方以为自己只是查了一下。
        """
        return self.root / f"{PROJECT_DIR_PREFIX}{project_id}"

    def ensure_project_dir(self, project_id: int) -> Path:
        path = self.project_dir(project_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def describe(self) -> dict[str, Any]:
        """给 ``GET /api/sandbox`` 用：能力与边界**一起**返回。"""
        return {
            "root": str(self.root),
            "timeout_s": self.timeout_s,
            "max_output_bytes": self.max_output_bytes,
            "network": self.network,
            "enforced": list(ENFORCED),
            "not_implemented": list(NOT_IMPLEMENTED),
        }


def _normcase_str(path: Path) -> str:
    """Windows 下路径比较必须归一化大小写（``C:\\Temp`` 与 ``c:\\temp`` 是同一个目录）。"""
    return os.path.normcase(str(path))


def is_within(child: Path, parent: Path) -> bool:
    """``child`` 是否在 ``parent`` 之内（含自身）。跨盘符返回 ``False``。

    用 ``os.path.commonpath`` 而不是 ``Path.is_relative_to``：后者在不同盘符上抛
    ``ValueError`` 且不做大小写归一 —— 恰好是 Windows 上最容易漏的两个分支。
    """
    try:
        common = os.path.commonpath([_normcase_str(child), _normcase_str(parent)])
    except ValueError:  # 不同盘符（Windows）或混合绝对/相对（理论上不该发生）
        return False
    return common == _normcase_str(parent)


def resolve_within(
    policy: SandboxPolicy,
    project_id: int,
    raw: str,
    *,
    must_exist: bool = False,
    for_write: bool = False,
) -> Path:
    """把模型给的路径解析成沙箱内的绝对路径；越界抛 ``SandboxViolation``。

    **解析顺序是刻意的**：先 ``resolve()``（它会展开 ``..`` 并跟随符号链接），
    再比对边界。反过来（先查字符串里有没有 ``..``）挡不住两种真实逃逸：
    指向外部的符号链接、以及 ``a/../..`` 这种语法上不含 ``..x`` 的写法。
    """
    if raw is None or not str(raw).strip():
        raise SandboxViolation(f"路径不能为空（project_id={project_id}）")

    base = policy.project_dir(project_id)
    text = str(raw).strip()
    candidate = Path(text)

    # 绝对路径 / 盘符路径 / 根路径一律拒（**包括指向沙箱内部的绝对路径**）。
    #
    # 这不是多余的严格：``Path("C:/a/project-7") / Path("/x")`` 会**丢掉**左边
    # 那一段（Windows 上带 root 的右侧路径直接替换），于是「项目根」这个前提
    # 在拼接那一刻就没了。跨盘符的 ``C:foo`` 更隐蔽：它不是绝对路径，
    # 却会按 C 盘当前目录解析 —— 那是调用方控制不了、我们也没打算暴露的一个坐标。
    #
    # 判据用 ``drive``/``root`` 而不是只判 ``is_absolute()``：后者在 Windows 上
    # 对 ``/x`` 和 ``C:x`` 都给 ``False``，恰好漏掉这两类。
    if candidate.is_absolute() or candidate.drive or candidate.root:
        raise SandboxViolation(
            f"路径必须是相对项目沙箱目录的相对路径：{text}"
            "（绝对路径、盘符路径与根路径一律不接受 —— 「在哪个项目的沙箱里」"
            "这个前提由项目上下文给出，不由调用方指定）"
        )

    candidate = base / candidate

    # resolve() 展开 .. 与符号链接，strict=False 允许目标尚不存在（写入场景）
    try:
        resolved = candidate.resolve()
    except OSError as exc:  # 例如路径过长、非法字符
        raise SandboxViolation(f"路径无法解析：{text}（{exc}）") from exc

    if not is_within(resolved, base):
        raise SandboxViolation(
            f"路径越出沙箱：{text} → {resolved}（允许的根：{base}）"
        )
    if must_exist and not resolved.exists():
        raise SandboxViolation(f"文件不存在：{text}")
    if for_write:
        # 写路径上额外挡住「父目录是个文件」这类会以 OSError 冒出来的情况，
        # 让错误在闸门处就带上「这是路径问题而不是命令问题」的语义
        parent = resolved.parent
        if parent.exists() and not parent.is_dir():
            raise SandboxViolation(f"父路径不是目录：{parent}")
    return resolved


def relative_to_project(policy: SandboxPolicy, project_id: int, path: Path) -> str:
    """把沙箱内绝对路径转回相对路径，用于落库与展示。

    落**相对**路径而不是绝对路径：绝对路径把本机的用户名与盘符写进审计记录，
    换台机器回放时那些路径全部失效，而相对路径仍然指向同一个产物。
    """
    base = policy.project_dir(project_id)
    if not is_within(path, base):
        raise SandboxViolation(f"路径不在沙箱内，无法取相对路径：{path}")
    rel = os.path.relpath(_normcase_str(path), _normcase_str(base))
    if rel == ".":
        return ""
    # Windows 的 relpath 用反斜杠；写进审计与 JSON 统一成正斜杠
    return rel.replace("\\", "/")


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """产物的 SHA256（设计 §10.1「产物哈希」）。

    分块读：实验产物可能是几百 MB 的数据文件，一次性读进内存会把一个正常的写操作
    变成 OOM。
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    """命令输出的哈希。产物可能不是文件（stdout 也是产出）。"""
    return hashlib.sha256(payload).hexdigest()


#: 截断标记预留的字节额度。标记形如 ``\n…[已截断 12345 字节]…\n``，
#: 即使 dropped 有 19 位也放得下。
TRUNCATION_RESERVE = 64

#: 头尾分配比例：头多尾少。头部是主体，尾部通常是错误栈或汇总行。
TRUNCATION_HEAD_RATIO = 0.6


def truncation_budgets(limit: int) -> tuple[int, int]:
    """头尾各自的字节预算（已扣掉标记的位置）。

    单独抽出来是因为**有两个读者**：字符串版（``truncated_output``）与文件版
    （命令输出走临时文件，只 seek 读两段）。两处各算一遍比例的结果是，
    命令输出与工具结果对「截断」给出两种措辞 —— 而同一次运行里出现两种说法，
    读日志的人得先判断哪个才是真的。
    """
    budget = max(2, limit - TRUNCATION_RESERVE)
    head = max(1, int(budget * TRUNCATION_HEAD_RATIO))
    return head, max(1, budget - head)


def truncation_marker(dropped: int) -> str:
    return f"\n…[已截断 {dropped} 字节]…\n"


def truncated_output(raw: str, limit: int) -> tuple[str, bool]:
    """按字节截断并**写明丢了多少**（与 D10 同一条原则）。

    头尾都留：命令输出的头部通常是「开始了什么」，尾部通常是错误栈或总结，
    只砍尾部会把最有诊断价值的部分丢掉。
    """
    encoded = raw.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return raw, False
    head_budget, tail_budget = truncation_budgets(limit)
    head = encoded[:head_budget].decode("utf-8", errors="ignore")
    tail = encoded[-tail_budget:].decode("utf-8", errors="ignore")
    dropped = len(encoded) - head_budget - tail_budget
    return f"{head}{truncation_marker(dropped)}{tail}", True
