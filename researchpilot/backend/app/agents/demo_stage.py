from __future__ import annotations

import json

from app.ai.base import ChatMessage
from app.orchestration.base import BlackboardWrite, StageAgent
from app.orchestration.orchestrator import StageRegistry
from app.store.dao import projects as projects_dao

# 8 个阶段：S1 已接入真实模型调用（Sprint 2 演示路径），S2–S8 为占位实现
STAGE_DEFS = [
    ("S1", "scout", "选题发现", "扫描领域图谱寻找空白点，生成候选研究问题"),
    ("S2", "librarian", "文献综述", "多源检索、解析、精读卡片与带溯源问答"),
    ("S3", "formalizer", "假设形式化", "候选问题转化为可证伪假设与变量表"),
    ("S4", "designer", "实验设计", "实验方案、对照组与样本量估算"),
    ("S5", "executor", "执行采集", "条件执行：真实跑实验或交付方案包"),
    ("S6", "analyst", "分析解读", "统计检验、图表与结果对比"),
    ("S7", "writer", "写作成稿", "带证据锚点的结构化草稿"),
    ("S8", "publisher", "投稿复现", "期刊匹配、复现包打包与校验"),
]

QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
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
    def __init__(self, stage_id: str, agent_id: str, name: str, description: str) -> None:
        self.stage_id = stage_id
        self.agent_id = agent_id
        self.name = name
        self.description = description

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
        questions = json.loads(response.text).get("questions", [])
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
    for stage_id, agent_id, name, description in STAGE_DEFS:
        if stage_id == "S1":
            registry.register(ScoutStage())
        else:
            registry.register(DemoStage(stage_id, agent_id, name, description))
