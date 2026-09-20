from __future__ import annotations

from app.ai.base import ChatMessage
from app.ai.json_utils import extract_json
from app.orchestration.base import BlackboardWrite, StageAgent
from app.orchestration.orchestrator import StageRegistry
from app.store.dao import projects as projects_dao

# 8 个阶段：(stage_id, agent_id, name, description, implemented, planned_sprint)
# S1 已接入真实模型调用；S2–S8 为占位实现，planned_sprint 为计划交付冲刺（US-307）
STAGE_DEFS = [
    ("S1", "scout", "选题发现", "扫描领域图谱寻找空白点，生成候选研究问题", True, None),
    ("S2", "librarian", "文献综述", "多源检索、解析、精读卡片与带溯源问答", False, 6),
    ("S3", "formalizer", "假设形式化", "候选问题转化为可证伪假设与变量表", False, 7),
    ("S4", "designer", "实验设计", "实验方案、对照组与样本量估算", False, 8),
    ("S5", "executor", "执行采集", "条件执行：真实跑实验或交付方案包", False, 9),
    ("S6", "analyst", "分析解读", "统计检验、图表与结果对比", False, 10),
    ("S7", "writer", "写作成稿", "带证据锚点的结构化草稿", False, 11),
    ("S8", "publisher", "投稿复现", "期刊匹配、复现包打包与校验", False, 11),
]

QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "rationale": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["question", "rationale", "score"],
            },
        }
    },
    "required": ["questions"],
}


class DemoStage(StageAgent):
    """占位阶段：产出占位黑板对象后即完成，`implemented=False` 供界面置灰。"""

    def __init__(self, stage_id: str, agent_id: str, name: str, description: str,
                 planned_sprint: int | None = None) -> None:
        self.stage_id = stage_id
        self.agent_id = agent_id
        self.name = name
        self.description = description
        self.implemented = False
        self.planned_sprint = planned_sprint

    def run(self, ctx) -> list[BlackboardWrite]:  # noqa: ANN001
        ctx.think(f"[{self.stage_id} {self.name}] 占位阶段运行：{self.description}")
        ctx.decide(f"{self.stage_id} 为 Sprint 占位实现，产出占位黑板对象后即完成。")
        return [
            BlackboardWrite(
                obj_type="stage_output",
                payload={
                    "stage_id": self.stage_id,
                    "stage_name": self.name,
                    "agent_id": self.agent_id,
                    "status": "placeholder",
                },
                evidence=[],
            )
        ]


QUESTIONS_FIELDS = ("question", "rationale", "score")


def _parse_questions(ctx, raw: str) -> list[dict]:  # noqa: ANN001
    """解析 S1 的结构化输出；任何不合规都**抛错**，绝不返回空列表。

    旧实现 `parsed.get("questions", [])` 在模型返回别的形状（真实事故：
    `{"response": "……", "format": "JSON"}`）时静默返回 `[]`，于是写下空的黑板对象、
    报 `stage.succeeded` —— 用户等了数分钟只换来「成功但没结果」。宁可失败并说清原因：
    失败会走 `run_stage` 的 `except` 分支，落 `failed_attempt` 决策 + checkpoint，
    最终以 `job.failed` 事件（带原因）推到前端。
    """
    try:
        parsed = extract_json(raw)
    except ValueError as exc:
        ctx.record("error", {"text": f"S1 结构化输出解析失败：{exc}", "raw": raw})
        raise ValueError(f"S1 结构化输出解析失败：{exc}") from None
    if not isinstance(parsed, dict):
        ctx.record("error", {"text": "S1 结构化输出不是 JSON 对象", "raw": raw})
        raise ValueError("S1 结构化输出不是 JSON 对象")

    questions = parsed.get("questions")
    if not isinstance(questions, list):
        got = "、".join(sorted(parsed)) or "（空对象）"
        ctx.record("error", {
            "text": f"S1 输出缺少 questions 数组（实际字段：{got}）", "raw": raw,
        })
        raise ValueError(f"S1 输出缺少 questions 数组（实际字段：{got}）")
    if not questions:
        ctx.record("error", {"text": "S1 输出的 questions 为空", "raw": raw})
        raise ValueError("S1 输出的 questions 为空，未产出候选研究问题")

    for index, item in enumerate(questions):
        if not isinstance(item, dict):
            ctx.record("error", {"text": f"S1 第 {index + 1} 个候选不是对象", "raw": raw})
            raise ValueError(f"S1 第 {index + 1} 个候选不是对象")
        missing = [f for f in QUESTIONS_FIELDS if not item.get(f)]
        if missing:
            ctx.record("error", {
                "text": f"S1 第 {index + 1} 个候选缺字段 {'、'.join(missing)}", "raw": raw,
            })
            raise ValueError(f"S1 第 {index + 1} 个候选缺字段 {'、'.join(missing)}")
    return questions


class ScoutStage(StageAgent):
    """S1 选题发现：plan 档位结构化生成候选研究问题（Sprint 2 演示路径）。"""

    stage_id, agent_id, name = "S1", "scout", "选题发现"
    description = "扫描领域图谱寻找空白点，生成候选研究问题"

    def run(self, ctx) -> list[BlackboardWrite]:  # noqa: ANN001
        ctx.think("[S1 选题发现] 调用 plan 档位模型，结构化生成候选研究问题")
        project = projects_dao.get(ctx.session, ctx.project_id)
        goal = (project.goal if project else "") or "通用研究目标"
        response = ctx.llm(
            "plan",
            messages=[
                ChatMessage(
                    role="system",
                    content="你是选题发现专家。根据研究目标生成候选研究问题，"
                            "每个问题包含 question / rationale / score（0-1 可行性）。",
                ),
                ChatMessage(role="user", content=f"研究目标：{goal}\n请生成 3 个候选研究问题。"),
            ],
            schema=QUESTIONS_SCHEMA,
        )
        questions = _parse_questions(ctx, response.text)
        ctx.decide(
            f"生成 {len(questions)} 个候选研究问题",
            reason="plan 档位结构化输出，待 S3 形式化为可证伪假设",
        )
        return [
            BlackboardWrite(
                obj_type="research_questions",
                payload={"questions": questions, "model": response.model},
                evidence=[],
            )
        ]


def register_all(registry: StageRegistry) -> None:
    for stage_id, agent_id, name, description, implemented, planned_sprint in STAGE_DEFS:
        if implemented:
            registry.register(ScoutStage())
        else:
            registry.register(
                DemoStage(stage_id, agent_id, name, description,
                          planned_sprint=planned_sprint)
            )
