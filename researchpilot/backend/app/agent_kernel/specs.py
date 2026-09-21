"""Agent 契约（US-404）—— 对齐设计 **§5.3 `AgentSpec`**。

§5.3 给了九个字段，本文件让它们**在代码里各有其位**，而不是继续停留在设计文档的代码块里：

| 字段 | 本步是否落地 | 消费者 |
|---|---|---|
| `id` / `stage` / `tier` | ✅ 声明并与既有定义**对钉**（见测试） | 编排、路由、agents 表播种 |
| `tools` | ✅ 收口来源：``agents.tools`` 由此播种，白名单由此判断 | 第 4 步（本步）；第 5 步调用点 |
| `max_steps` / `max_cost_usd` | ✅ 播种进 ``agents.budget_steps`` / ``budget_cost`` | ``BudgetManager``（已有） |
| `requires_critic` / `human_checkpoint` | ✅ 声明 | 阶段二（Critic 接线）与第 6 步（中断点） |
| `reads` / `writes` | ✅ 声明 | 第 6 步（黑板对象类型权限） |

⚠️ **``tools`` 的价值主要是否向断言**。今天八个阶段 Agent 的白名单里都只有
``run_pipeline``（编排模板的每一步都要它），看起来像「全放行」；但 §5.3 的
权限最小化原则说的是**哪些工具不在里面** —— 处理文献内容的 Agent 不持有文件写权限、
不持有网络工具、不持有密钥。第 6 步加进 ``run_command`` / ``read_file`` 时，
只有 ``executor`` 能拿到，其余七个的白名单必须**仍然不含**它们（有测试钉住）。
把白名单做成「正向列举」而不是「不做」，意义就在这里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent_kernel.errors import KernelError

#: 中断点时机（§6.5 按风险分级）。字段取值与设计 §5.3 的 Literal 一致。
CHECKPOINT_NONE = "none"          # 自动通过，事后可审
CHECKPOINT_BEFORE = "before"      # 事前批：高风险动作，不可配置关闭
CHECKPOINT_AFTER = "after"        # 事后审：产出后必须人工确认才进入下一阶段
CHECKPOINT_RISK_BASED = "risk_based"  # 运行时按风险等级动态判定
HUMAN_CHECKPOINTS: tuple[str, ...] = (
    CHECKPOINT_NONE, CHECKPOINT_BEFORE, CHECKPOINT_AFTER, CHECKPOINT_RISK_BASED,
)


@dataclass(frozen=True)
class AgentSpec:
    """一个 Agent 的全部契约。**冻结**：它是代码资产，不是运行期配置。

    ``tools`` / ``reads`` / ``writes`` 用 ``tuple`` 而不是 ``list``：冻结的数据类里放
    可变容器，「不可变」就只停在字段级 —— 拿到手仍能往里塞，而哈希与相等也一起坏掉。
    """

    id: str
    stage: str
    tier: str
    tools: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    max_steps: int = 20
    max_cost_usd: float = 2.0
    requires_critic: bool = True
    human_checkpoint: str = CHECKPOINT_NONE

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise KernelError("AgentSpec.id 不能为空")
        if self.human_checkpoint not in HUMAN_CHECKPOINTS:
            raise KernelError(
                f"Agent {self.id} 的 human_checkpoint 非法：{self.human_checkpoint}"
                f"（可选 {'、'.join(HUMAN_CHECKPOINTS)}）"
            )
        if self.max_steps <= 0:
            raise KernelError(f"Agent {self.id} 的 max_steps 必须为正")
        if self.max_cost_usd < 0:
            raise KernelError(f"Agent {self.id} 的 max_cost_usd 不能为负")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage,
            "tier": self.tier,
            "tools": list(self.tools),
            "reads": list(self.reads),
            "writes": list(self.writes),
            "max_steps": self.max_steps,
            "max_cost_usd": self.max_cost_usd,
            "requires_critic": self.requires_critic,
            "human_checkpoint": self.human_checkpoint,
        }


#: 八个阶段 Agent 的契约。
#:
#: - ``tier`` 与 ``agents_dao.STAGE_TIERS`` 必须一致（测试对钉，防止两处各改一半）
#: - ``max_steps`` / ``max_cost_usd`` 暂与既有 ``agents`` 表默认值相同（20 / 2.0）：
#:   本步只是把「契约有个来源」这件事做出来，**不借机改预算行为** ——
#:   顺手调预算会让一次纯结构改动带上无法归因的运行差异
#: - ``reads`` / ``writes`` 取各阶段设计（§7.1–§7.8）「输出」一栏的对象名
#: - ``human_checkpoint`` 按 §6.5 风险分级：高（真实执行、写作成稿、对外发送）→ 事前批；
#:   中（假设定稿、方案确定、结果解读）→ 事后审
STAGE_AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        id="scout", stage="S1", tier="plan",
        # 高：**选定研究问题**是整条链上最贵的一次决策，选错了后面全白做（§6.5）
        tools=("run_pipeline",),
        writes=("research_questions",),
        human_checkpoint=CHECKPOINT_BEFORE,
    ),
    AgentSpec(
        id="librarian", stage="S2", tier="synthesize",
        tools=("run_pipeline",),
        reads=("research_questions",),
        writes=("literature_pool", "reading_cards", "knowledge_map", "search_strategy"),
        human_checkpoint=CHECKPOINT_AFTER,
    ),
    AgentSpec(
        id="formalizer", stage="S3", tier="plan",
        tools=("run_pipeline",),
        reads=("research_questions", "literature_pool"),
        writes=("hypothesis", "variable_table"),
        human_checkpoint=CHECKPOINT_AFTER,
    ),
    AgentSpec(
        id="designer", stage="S4", tier="plan",
        tools=("run_pipeline",),
        reads=("hypothesis", "variable_table"),
        writes=("experiment_plan",),
        human_checkpoint=CHECKPOINT_AFTER,
    ),
    AgentSpec(
        id="executor", stage="S5", tier="extract",
        # 第 6 步会把沙箱里的读写与 run_command 只挂到这一个 Agent 上（§5.3 权限最小化）
        tools=("run_pipeline",),
        reads=("experiment_plan",),
        writes=("execution_result", "artifact"),
        # 高：**真实执行实验**有真实副作用与真实开销
        human_checkpoint=CHECKPOINT_BEFORE,
    ),
    AgentSpec(
        id="analyst", stage="S6", tier="synthesize",
        tools=("run_pipeline",),
        reads=("execution_result",),
        writes=("analysis_report", "figures"),
        human_checkpoint=CHECKPOINT_AFTER,
    ),
    AgentSpec(
        id="writer", stage="S7", tier="write",
        tools=("run_pipeline",),
        reads=("analysis_report", "literature_pool"),
        writes=("manuscript",),
        # 高：**写正式文件**
        human_checkpoint=CHECKPOINT_BEFORE,
    ),
    AgentSpec(
        id="publisher", stage="S8", tier="synthesize",
        tools=("run_pipeline",),
        reads=("manuscript",),
        writes=("submission_package",),
        # 高：**对外发送**
        human_checkpoint=CHECKPOINT_BEFORE,
    ),
)

#: 会话内核的契约（US-405）。**不属于任何科研阶段**（``stage="chat"``）：
#: 它是「用户在一个会话里说话、内核回应」这条路径上的执行者，与 S1–S8 的领域 Agent
#: 是两回事 —— 后者有阶段产物与阶段档位，前者只有对话与工具调用。
#:
#: 工具面**刻意最小**。第 6 步把沙箱读写与 ``run_command`` 只挂到 ``executor`` 上，
#: 届时会话要动用它们必须**显式声明** ``agent_id=executor`` ——「默认最严、提权要写明」
#: 正是 §5.3 的最小权限原则。反过来的默认（会话默认拿全部工具）会让「我只是问了个问题」
#: 也能触发一次真实的命令执行。
#:
#: ``requires_critic=False`` / ``human_checkpoint=NONE``：会话是人机同步交互的，
#: 人就在屏幕前，再插一道事中检查点只是把同一件事问两遍。危险**动作**的闸门挂在
#: 工具权限上（第 6 步），不挂在会话上。
CONVERSATION_SPEC = AgentSpec(
    id="kernel", stage="chat", tier="plan",
    tools=("run_pipeline",),
    max_steps=20,
    max_cost_usd=2.0,
    requires_critic=False,
    human_checkpoint=CHECKPOINT_NONE,
)

#: 全部契约（阶段 Agent + 会话内核）。``agents_allowing`` / ``unknown_tools`` 用它，
#: 因为它们回答的是「谁能调这个工具」—— 漏掉会话内核，``GET /api/tools`` 就会
#: 声称 run_pipeline 无人可用，而会话明明能调它。
ALL_AGENT_SPECS: tuple[AgentSpec, ...] = STAGE_AGENT_SPECS + (CONVERSATION_SPEC,)

#: 支撑 Agent（§5.2）**本步不建 spec**：Critic / Curator / Steward / Human 目前都还没有
#: 执行体（阶段二交付）。先写一份没人消费、也无从验证的契约，只会让「设计已对齐」变成
#: 一种错觉 —— 它们各自的字段含义（评审阈值、记忆淘汰策略、审批边界）要等实现时才定得准。
BY_AGENT_ID: dict[str, AgentSpec] = {spec.id: spec for spec in ALL_AGENT_SPECS}
#: 按阶段查**只映射阶段 Agent**：``"chat"`` 不是一个科研阶段，放进来会让
#: 「S1–S8 是否齐全」这类检查把会话内核也算进去。
BY_STAGE: dict[str, AgentSpec] = {spec.stage: spec for spec in STAGE_AGENT_SPECS}


def by_agent_id(agent_id: str) -> AgentSpec | None:
    return BY_AGENT_ID.get(agent_id)


def by_stage(stage_id: str) -> AgentSpec | None:
    return BY_STAGE.get(stage_id)


def agents_allowing(tool_name: str) -> list[str]:
    """哪些 Agent 的白名单里有这个工具（``GET /api/tools`` 的 ``allowed_agents``）。

    返回有序列表：界面按它渲染权限徽标，顺序稳定才不会有「每次刷新顺序都变」的观感。
    """
    return [spec.id for spec in ALL_AGENT_SPECS if tool_name in spec.tools]


def unknown_tools(registered: set[str]) -> dict[str, list[str]]:
    """找出「白名单里写了、注册表里没有」的工具，返回 ``{工具名: [Agent...]}``。

    装配期**只告警不抛错**：与 ``Router.from_config`` 同一条约定（加载路径宽容，
    交互路径严格）。阶段二某个领域包还没装、工具还没实现，不应该让整个应用起不来。
    """
    missing: dict[str, list[str]] = {}
    for spec in ALL_AGENT_SPECS:
        for tool in spec.tools:
            if tool not in registered:
                missing.setdefault(tool, []).append(spec.id)
    return missing
