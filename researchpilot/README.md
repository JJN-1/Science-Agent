# ResearchPilot · 科研全链路多 Agent 系统

覆盖科研全流程的本地多 Agent 协作系统：选题发现 → 文献综述 → 假设形式化 → 实验设计 → 执行采集 → 分析解读 → 写作成稿 → 投稿复现。

- 技术设计方案：`../科研全链路多Agent系统_技术设计方案.md`
- 敏捷排期：`../科研全链路多Agent系统_敏捷冲刺排期.md`
- Sprint 1 实施计划：`../Sprint1_实施计划.md`
- Sprint 2 实施计划：`../Sprint2_实施计划.md`

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12+ · FastAPI · SQLAlchemy 2.0 · Alembic · SQLite（WAL） |
| 前端 | React 18+ · TypeScript · Vite · Ant Design 5+ |
| AI 接入 | OpenAI 兼容端点（httpx）· keyring（凭据管理器存 Key） |
| 工具 | uv（Python 依赖）· npm（前端依赖），全部项目内本地依赖 |

## 目录

```
researchpilot/
├── backend/            # FastAPI 服务（app/ + migrations/ + tests/）
│   └── app/ai/         # AI 接入层：registry / routing / degrade / budget / client
├── frontend/           # React 前端（会话式单流界面 + src/components）
└── config/default.yaml # 默认配置（用户配置在数据目录 config.yaml 覆盖）
```

## 快速开始

### 后端（端口 8000）

```bash
cd backend
python -m uv sync          # 创建 .venv 并安装依赖
python -m uv run pytest    # 运行测试（46 个用例）
python -m uv run uvicorn app.main:app --reload
```

首次启动会自动创建数据目录 `%APPDATA%\ResearchPilot\`（app.db / files / tex / workspace / packs / logs / config.yaml）并执行数据库迁移（US-102 零配置首启）。

### 前端（端口 5173，代理 /api → 8000）

```bash
cd frontend
npm install --include=dev  # 本机若设置了 omit=dev 必须加 --include=dev
npm run dev
```

访问 http://localhost:5173 ：左侧选项目 → 底部命令行 `run S1` 回车 → 转录流实时呈现 Agent 步骤（`⏺` / `⎿`）。

## 模型接入（Sprint 2）

- 默认零配置指向 **mock provider**，可完整演示档位路由、成本记账、预算熔断与审批
- 接真实模型：左侧「⚙ 设置 · 模型后端」→ 录入 API Key（写入 **Windows 凭据管理器**，不落盘明文）→ 在用户 `config.yaml` 增加 `openai_compat` 类型 provider → 修改档位路由 → 「⟳ 热重载」立即生效，无需重启
- 五档位路由：`extract` / `plan` / `critique` / `synthesize` / `write`，critique 档强制跨厂商（交叉验证约束）
- 能力降级：缺 JSON 模式自动注入 schema 至 prompt，校验失败携错重试一次；全程记录 `degraded` 标记
- 预算治理：项目总额/每日额度 + Agent 级步数/成本上限；超限 run 自动**暂停**并在流内弹出审批卡片，批准后恢复重跑

## 已实现

**Sprint 1（US-101 ~ 105）**：脚手架与零配置首启、五表数据模型与 DAO、Orchestrator（注册/调度/检查点）、轨迹查看与回放。

**Sprint 2（US-201 ~ 206）**：
- 多后端接入与档位路由（US-201）：`app/ai/` Provider 抽象 + Registry + Router
- 能力降级链（US-202）：schema 注入 / 携错重试 / 降级标记落库
- 成本记账与三维归因（US-203）：`llm_usage` 表按项目/阶段/Agent 汇总
- 决策与失败尝试日志（US-204）：`decisions` 表，Agent 手动 + 系统自动
- 预算熔断与审批（US-205）：暂停 + 流内审批卡片 + 批准后恢复
- 热重载与健康状态（US-206）：providers reload / routing PATCH / 切换审计

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET/POST | `/api/projects` | 项目列表 / 创建 |
| GET/PATCH | `/api/projects/{id}` | 项目详情 / 更新 |
| GET | `/api/stages` | 阶段注册表 |
| POST | `/api/projects/{id}/stages/{sid}/run` | 运行阶段（返回 run_id + 状态） |
| GET | `/api/projects/{id}/runs` / `/api/runs/{id}` | 运行记录 / 轨迹明细 |
| GET | `/api/projects/{id}/blackboard` / `/checkpoints` | 黑板对象 / 检查点 |
| GET | `/api/projects/{id}/decisions` | 决策与失败尝试日志 |
| GET | `/api/usage/summary?project_id=&dim=` | 成本三维归因 |
| GET/POST | `/api/approvals` · `/api/approvals/{id}/approve|reject` | 审批流转 |
| GET | `/api/settings/providers` · `/api/settings/routing` | 后端健康 / 档位路由 |
| POST | `/api/settings/providers/reload` | 全局热重载 |
| PATCH | `/api/settings/routing` | 档位热切换 |
| PUT | `/api/settings/providers/{name}/key` | Key 写入凭据管理器 |

## 说明

- 数据可用环境变量 `RESEARCHPILOT_DATA_DIR` 重定向（测试使用）。
- 阶段 Agent：S1 已接入真实模型调用（plan 档结构化生成研究问题），S2–S8 为占位实现，Sprint 3 起逐个替换。
