"""结构化任务计划与计划器（US-403）。

**计划必须是结构化的，不能是自由文本。** 两个理由各自独立，缺一条都不值得为它建表：

1. 阶段二的科研模式等于**替换计划模板**（两阶段衔接约定 2）。文本计划没法「只换模板」——
   模板和提示词会混在一起，之后无法验证模板本身对不对。
2. G2 第 7 条要断言「同一输入两次跑出相同步骤序列」。文本计划只能比字符串，
   那会把「换了个措辞」也判成不一致；判据必须落在结构上。

三种计划来源，同一个出口 ``Plan``：**模板** / **模型提案** / **人工修改**。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.agent_kernel.errors import PlanError
from app.ai.schema_utils import schema_errors
from app.observability.logging import get_logger

logger = get_logger("agent_kernel.planner")

#: 确定性模式下使用的固定种子（D7）。落库，事后才证明得了「这次是确定性的」
DETERMINISTIC_SEED = 0

PLAN_EXECUTE = "plan_execute"
REACT = "react"
#: 与 ``store.models.PLAN_MODES`` 同步（有单测钉住）。刻意不 import store：
#: 内核层不该反向依赖存储层，而为此把常量塞进一个中立模块也不值当。
VALID_MODES = (PLAN_EXECUTE, REACT)

#: 步骤状态，与 ``store.models.PLAN_STEP_STATUSES`` 同步（同样有单测）
VALID_STEP_STATUSES = ("pending", "running", "done", "failed", "skipped")

#: 只有 draft 可改：批准等于冻结，执行中的计划改了会让检查点引用到不存在的东西
EDITABLE_STATUSES = ("draft",)

#: 模型提案的返回类型：吃一个研究目标，吐一个待校验的 dict
Proposer = Callable[[str], dict]


@dataclass(frozen=True)
class PlanStep:
    """计划中的一步。

    ``id`` 是**稳定标识**而不是序号：检查点要靠它回答「从哪一步继续」，
    用下标的话，中间插一步就让所有历史检查点错位。
    """

    id: str
    title: str
    intent: str = ""
    tool: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "intent": self.intent,
            "tool": self.tool,
            "params": dict(self.params),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> PlanStep:
        if not isinstance(raw, dict):
            raise PlanError(f"步骤必须是对象，实际为 {type(raw).__name__}")
        step_id = str(raw.get("id") or "").strip()
        if not step_id:
            raise PlanError("步骤缺少 id（检查点要靠它定位，不能为空）")
        title = str(raw.get("title") or "").strip()
        if not title:
            raise PlanError(f"步骤 {step_id} 缺少 title")

        tool = raw.get("tool")
        tool = str(tool).strip() if isinstance(tool, str) and tool.strip() else None

        params = raw.get("params") or {}
        if not isinstance(params, dict):
            raise PlanError(f"步骤 {step_id} 的 params 必须是对象")

        status = str(raw.get("status") or "pending")
        if status not in VALID_STEP_STATUSES:
            raise PlanError(
                f"步骤 {step_id} 的状态非法：{status}"
                f"（可选 {'、'.join(VALID_STEP_STATUSES)}）"
            )

        return cls(
            id=step_id,
            title=title,
            intent=str(raw.get("intent") or ""),
            tool=tool,
            params=dict(params),
            status=status,
        )


@dataclass(frozen=True)
class Plan:
    """与 ``task_plans`` 表一一对应的计划对象（鸭子类型读行，内核不反向依赖 store）。"""

    steps: tuple[PlanStep, ...] = ()
    mode: str = PLAN_EXECUTE
    deterministic: bool = False
    seed: int | None = None
    title: str = ""
    rationale: str = ""

    def to_steps_payload(self) -> list[dict[str, Any]]:
        return [step.to_dict() for step in self.steps]

    @property
    def step_ids(self) -> tuple[str, ...]:
        return tuple(step.id for step in self.steps)

    def counts(self) -> dict[str, int]:
        """各状态计数，供进度展示与「跑完了没有」的判断。"""
        tally = {status: 0 for status in VALID_STEP_STATUSES}
        for step in self.steps:
            tally[step.status] += 1
        return tally

    @classmethod
    def from_row(cls, row: Any) -> Plan:
        """从 ``task_plans`` 表的一行构造。

        这里**不重复做写入口的那套校验**：数据在写入时已经校验过，
        读的时候再校验一遍只会把「库里有脏数据」变成 500。脏数据该被看见，
        但不该在读取路径上炸掉整个计划。
        """
        raw_steps = getattr(row, "steps", None) or []
        return cls(
            steps=tuple(PlanStep.from_dict(item) for item in raw_steps),
            mode=getattr(row, "mode", PLAN_EXECUTE) or PLAN_EXECUTE,
            deterministic=bool(getattr(row, "deterministic", False)),
            seed=getattr(row, "seed", None),
            title=getattr(row, "title", "") or "",
            rationale=getattr(row, "rationale", "") or "",
        )


@dataclass(frozen=True)
class PlanTemplate:
    template_id: str
    title: str
    rationale: str
    steps: tuple[PlanStep, ...]


#: 科研全链路编排模板（启动顺序与 ``orchestration.orchestrator.STAGE_ORDER``、
#: 名称与 ``agents.demo_stage.STAGE_DEFS`` 一致，有单测钉住防止漂移）。
#:
#: 每个阶段步骤都指向 ``run_pipeline``（两阶段衔接约定 1：编排要在内核里可达）。
#: 该工具由 Sprint 4 第 4 步注册 —— 模板先把它写下来，等于把这条约定变成可断言的。
RESEARCH_PIPELINE = PlanTemplate(
    template_id="research_pipeline",
    title="科研全链路（S1→S8 固定顺序）",
    rationale=(
        "设计 §6.3 的确定性编排：按既定阶段顺序推进，每阶段产结构化产物。"
        "回退边（Critic 否决后退回上游）属阶段二，不在本模板内。"
    ),
    steps=(
        PlanStep("S1", "选题发现", "扫描领域图谱寻找空白点，生成候选研究问题",
                 tool="run_pipeline", params={"stage_ids": ["S1"]}),
        PlanStep("S2", "文献综述", "多源检索、解析、精读卡片与带溯源问答",
                 tool="run_pipeline", params={"stage_ids": ["S2"]}),
        PlanStep("S3", "假设形式化", "候选问题转化为可证伪假设与变量表",
                 tool="run_pipeline", params={"stage_ids": ["S3"]}),
        PlanStep("S4", "实验设计", "实验方案、对照组与样本量估算",
                 tool="run_pipeline", params={"stage_ids": ["S4"]}),
        PlanStep("S5", "执行采集", "条件执行：真实跑实验或交付方案包",
                 tool="run_pipeline", params={"stage_ids": ["S5"]}),
        PlanStep("S6", "分析解读", "统计检验、图表与结果对比",
                 tool="run_pipeline", params={"stage_ids": ["S6"]}),
        PlanStep("S7", "写作成稿", "带证据锚点的结构化草稿",
                 tool="run_pipeline", params={"stage_ids": ["S7"]}),
        PlanStep("S8", "投稿复现", "期刊匹配、复现包打包与校验",
                 tool="run_pipeline", params={"stage_ids": ["S8"]}),
    ),
)

TEMPLATES: dict[str, PlanTemplate] = {RESEARCH_PIPELINE.template_id: RESEARCH_PIPELINE}


def get_template(template_id: str | None) -> PlanTemplate:
    if template_id is None:
        return RESEARCH_PIPELINE
    template = TEMPLATES.get(template_id)
    if template is None:
        raise PlanError(
            f"未知计划模板：{template_id}（可选 {'、'.join(sorted(TEMPLATES))}）"
        )
    return template


#: 模型提案要满足的形状。**schema 始终随 prompt 下发**是既有铁律：
#: 不给 schema 就要求模型产出结构，等于让它猜我们的字段名。
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "rationale": {"type": "string"},
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "intent": {"type": "string"},
                    "tool": {"type": "string"},
                    "params": {"type": "object"},
                },
                "required": ["id", "title"],
            },
        },
    },
    "required": ["steps"],
}


# ── 构造 ────────────────────────────────────────

def normalize_steps(raw_steps: Iterable[Any]) -> tuple[PlanStep, ...]:
    """校验并规范化步骤序列（模板 / 模型 / 人工三个入口共用）。

    **id 唯一性在这里把关**：检查点记的是 step id，重复 id 会让「从第 3 步继续」
    失去唯一解 —— 得到的是一份看起来能跑、但恢复时随机挑一个的执行计划。
    """
    steps = tuple(PlanStep.from_dict(raw) for raw in raw_steps)
    if not steps:
        raise PlanError("计划至少需要一个步骤")

    seen: set[str] = set()
    duplicates: set[str] = set()
    for step in steps:
        if step.id in seen:
            duplicates.add(step.id)
        seen.add(step.id)
    if duplicates:
        raise PlanError(
            f"步骤 id 重复：{'、'.join(sorted(duplicates))}"
            "（检查点靠 id 定位，重复即无法确定从哪一步恢复）"
        )
    return steps


def plan_from_payload(
    payload: Any, *, mode: str = PLAN_EXECUTE, deterministic: bool = False,
) -> Plan:
    """把模型给出的 dict 转成 Plan。形状不合规抛 ``AGENT-PLAN-001``（带违规路径）。"""
    errors = schema_errors(payload, PLAN_SCHEMA)
    if errors:
        raise PlanError("模型给出的计划不符合 schema：" + "；".join(errors[:5]))
    return Plan(
        steps=normalize_steps(payload["steps"]),
        mode=mode,
        deterministic=deterministic,
        title=str(payload.get("title") or ""),
        rationale=str(payload.get("rationale") or ""),
    )


def template_plan(
    template: PlanTemplate | None = None,
    *,
    goal: str = "",
    deterministic: bool = False,
    note: str = "",
) -> Plan:
    template = template or RESEARCH_PIPELINE
    rationale = template.rationale
    if goal:
        rationale = f"{rationale} 研究目标：{goal.strip()}"
    if deterministic:
        rationale = f"{rationale} 确定性模式：步骤序列由模板固定，未经模型改写。"
    if note:
        rationale = f"{rationale} {note}"
    return Plan(
        steps=template.steps,
        mode=PLAN_EXECUTE,
        deterministic=deterministic,
        seed=DETERMINISTIC_SEED if deterministic else None,
        title=template.title,
        rationale=rationale,
    )


def build_plan(
    *,
    goal: str = "",
    mode: str | None = None,
    deterministic: bool = False,
    template: PlanTemplate | None = None,
    propose: Proposer | None = None,
) -> Plan:
    """产出计划。三种来源的优先次序与降级都在这里说清楚。

    - ``deterministic=True``：**完全不问模型**，直接走模板。问一次就多一个不可复现的输入，
      而确定性模式的全部用途就是复现（设计 §12.4 消融实验）。
    - 否则：有 ``propose`` 就用模型提案；提案不可用则回退模板，**并把原因写进
      ``rationale``** —— 静默换成模板等于让用户以为「这是模型想出来的」。
    - ``react`` **不接受模板**：见 ``_template_fallback``。想做 react 必须给提案，
      否则抛 ``AGENT-PLAN-002``，而不是降级成 plan_execute。
    """
    chosen = mode or PLAN_EXECUTE
    if chosen not in VALID_MODES:
        raise PlanError(
            f"未知编排模式：{chosen}（可选 {'、'.join(VALID_MODES)}）"
        )
    if deterministic and chosen != PLAN_EXECUTE:
        raise PlanError(
            f"确定性模式不接受 {chosen}：D7 规定 deterministic 恒为 plan_execute。"
            "ReAct 的下一步由观察结果决定，与「固定顺序」在语义上互斥。",
            code="AGENT-PLAN-002",
        )

    if deterministic:
        return template_plan(template, goal=goal, deterministic=True)

    if propose is not None:
        try:
            plan = plan_from_payload(propose(goal), mode=chosen)
            return replace(plan, deterministic=False, seed=None,
                           title=plan.title or (template or RESEARCH_PIPELINE).title)
        except PlanError as exc:
            logger.warning("plan_proposal_rejected", error=str(exc))
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001 —— 提案失败不该让建计划这件事失败
            logger.warning("plan_proposal_failed", error=str(exc))
            reason = f"{type(exc).__name__}: {exc}"
        return _template_fallback(
            template, goal=goal, mode=chosen,
            note=f"（模型提案不可用，已回退编排模板：{reason}）",
        )

    return _template_fallback(template, goal=goal, mode=chosen)


def _template_fallback(
    template: PlanTemplate | None, *, goal: str, mode: str, note: str = "",
) -> Plan:
    """回退到模板。**模板与 plan_execute 绑定，不是默认值而是定义。**

    模板给的是一份预先写死的步骤序列，而 ``react`` 的下一步由观察结果决定 ——
    两者直接冲突。若在这里悄悄把 ``react`` 请求当成 ``plan_execute`` 建一份计划，
    用户会拿到一份「mode 写着 react、内容是固定八步」的计划：界面显示的是模型/用户的
    诉求，实际执行的是另一回事。这类「标注与行为不一致」正是 G2 第 8 条要防的东西。
    """
    if mode != PLAN_EXECUTE:
        raise PlanError(
            f"{mode} 模式没有预置步骤序列，而模板就是一份预置步骤序列 —— 两者互斥。"
            "ReAct 的下一步由观察结果决定，将在内核循环（第 5 步）接入。",
            code="AGENT-PLAN-002",
        )
    return template_plan(template, goal=goal, deterministic=False, note=note)


# ── 状态规则 ────────────────────────────────────

def ensure_editable(status: str) -> None:
    """只有 draft 可改。批准即冻结 ——

    执行中的计划被改动，会让已经落下的 ``kernel_checkpoints`` 引用到一份
    与执行时不同的计划，而检查点的全部意义就是「恢复到当时的状态」。
    """
    if status not in EDITABLE_STATUSES:
        raise PlanError(
            f"计划状态为 {status}，不可修改（仅 {'/'.join(EDITABLE_STATUSES)} 可改）。"
            "批准等于冻结：已执行过的计划被改动，检查点就指不回当时那份了。",
            code="AGENT-PLAN-003",
        )


def ensure_approvable(status: str) -> None:
    if status != "draft":
        raise PlanError(
            f"计划状态为 {status}，不可批准（仅 draft 可批准）",
            code="AGENT-PLAN-003",
        )


def revise_plan(
    plan: Plan,
    *,
    steps: Sequence[Any] | None = None,
    title: str | None = None,
    rationale: str | None = None,
) -> Plan:
    """人工修改。

    **确定性计划禁止改步骤，但允许改标题与说明。** 这条区分不是随口定的：
    执行只读 ``steps``，所以改标题/说明不影响可复现性；而一旦改了步骤，
    「同一输入两次相同结果」就不再成立，确定性模式也就失去意义。
    """
    if steps is not None and plan.deterministic:
        raise PlanError(
            "确定性计划不允许修改步骤（D7）：它的用途是复现与消融实验，"
            "而'自由度就是噪声'。需要改请另建一份非确定性计划。",
            code="AGENT-PLAN-003",
        )
    return replace(
        plan,
        steps=plan.steps if steps is None else normalize_steps(steps),
        title=plan.title if title is None else title,
        rationale=plan.rationale if rationale is None else rationale,
    )
