# ResearchPilot · 科研全链路多 Agent 系统

覆盖科研全流程的本地多 Agent 协作系统：选题发现 → 文献综述 → 假设形式化 → 实验设计 → 执行采集 → 分析解读 → 写作成稿 → 投稿复现。

- 技术设计方案：`../科研全链路多Agent系统_技术设计方案.md`
- 敏捷排期：`../科研全链路多Agent系统_敏捷冲刺排期.md`
- Sprint 1 实施计划：`../Sprint1_实施计划.md`

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12+ · FastAPI · SQLAlchemy 2.0 · Alembic · SQLite（WAL） |
| 前端 | React 18+ · TypeScript · Vite · Ant Design 5+ |
| 工具 | uv（Python 依赖）· npm（前端依赖），全部项目内本地依赖 |

## 目录

```
researchpilot/
├── backend/            # FastAPI 服务（app/ + migrations/ + tests/）
├── frontend/           # React 前端（src/pages 三页面 + src/components）
└── config/default.yaml # 默认配置（用户配置在数据目录 config.yaml 覆盖）
```

## 快速开始

### 后端（端口 8000）

```bash
cd backend
python -m uv sync          # 创建 .venv 并安装依赖
python -m uv run pytest    # 运行测试（11 个用例）
python -m uv run uvicorn app.main:app --reload
```

首次启动会自动创建数据目录 `%APPDATA%\ResearchPilot\`（app.db / files / tex / workspace / packs / logs / config.yaml）并执行数据库迁移（US-102 零配置首启）。

### 前端（端口 5173，代理 /api → 8000）

```bash
cd frontend
npm install --include=dev  # 本机若设置了 omit=dev 必须加 --include=dev
npm run dev
```

访问 http://localhost:5173 ：新建项目 → 阶段列表点「运行」→ 「查看轨迹」进入回放页。

## Sprint 1 已实现（US-101 ~ 105）

- 前后端脚手架与本地运行环境（US-101）
- 首次启动自动创建数据目录与配置（US-102）
- `projects` / `blackboard` / `stage_checkpoints` / `agent_runs` / `agent_steps` 数据模型与 DAO（US-103）
- Orchestrator：阶段注册表（S1–S8 占位）、顺序调度（`run_stage` / `run_pipeline`）、每阶段检查点（US-104）
- 轨迹查看与回放：RunDetailPage 时间线 + 1s 步进回放（US-105）

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET/POST | `/api/projects` | 项目列表 / 创建 |
| GET/PATCH | `/api/projects/{id}` | 项目详情 / 更新 |
| GET | `/api/stages` | 阶段注册表 |
| POST | `/api/projects/{id}/stages/{sid}/run` | 运行阶段（同步返回 run_id） |
| GET | `/api/projects/{id}/runs` / `/api/runs/{id}` | 运行记录 / 轨迹明细 |
| GET | `/api/projects/{id}/blackboard` / `/checkpoints` | 黑板对象 / 检查点 |

## 说明

- 数据可用环境变量 `RESEARCHPILOT_DATA_DIR` 重定向（测试使用）。
- 阶段 Agent 目前为占位实现（`backend/app/agents/demo_stage.py`），Sprint 3 起逐个替换。
