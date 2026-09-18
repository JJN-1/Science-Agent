# Sprint 1 实施计划：ResearchPilot 骨架与 Agent 运行时

**目标**（US-101~105，32 SP）：可启动的 FastAPI + Vite + SQLite 应用，能创建研究项目、注册并顺序调度阶段、保存检查点，前端可查看与回放 Agent 运行轨迹。
**演示验收**：启动应用 → 创建项目 → 触发一个空阶段 → 界面看到完整轨迹。

## 选型（已确认）

- Python 3.12+，uv 管理（pyproject.toml + uv.lock），依赖全部项目内本地安装
- FastAPI + Pydantic v2 + SQLAlchemy 2.0（同步 engine，SQLite WAL；JobWorker 异步化留 Sprint 2）
- Alembic 迁移；前端 npm + Vite + React 18 + TS + AntD 5（完整可演示）
- 数据目录 `platformdirs` 定位 `%APPDATA%\ResearchPilot\`（US-102 首启自动创建）

## 目录结构（对应技术设计方案 §15）

```
researchpilot/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口：lifespan 初始化 store/config/编排器
│   │   ├── config.py            # 配置加载：default.yaml + 用户 config.yaml 合并；首启建目录
│   │   ├── api/
│   │   │   ├── projects.py      # 项目 CRUD
│   │   │   ├── runs.py          # 轨迹查询 /api/runs/{id}, /steps
│   │   │   ├── stages.py        # 阶段触发 /api/projects/{id}/stages/{sid}/run
│   │   │   └── schemas.py       # Pydantic 请求/响应模型
│   │   ├── orchestration/
│   │   │   ├── orchestrator.py  # 阶段注册表 + 顺序调度 + 检查点写入
│   │   │   ├── base.py          # StageAgent 抽象基类（run(ctx) -> BlackboardWrite[]）
│   │   │   └── context.py       # StageContext：项目 id、黑板读写、轨迹记录句柄
│   │   ├── agents/
│   │   │   └── demo_stage.py    # 占位空阶段（S1..S8 占位，记录"思考"步骤用于演示轨迹）
│   │   ├── store/
│   │   │   ├── db.py            # engine/session（WAL、foreign_keys=ON）
│   │   │   ├── models.py        # ORM 表定义（见下）
│   │   │   └── dao/             # projects.py blackboard.py runs.py checkpoints.py
│   │   └── observability/logging.py  # structlog JSON 日志，trace_id
│   ├── migrations/              # Alembic（env.py + versions/0001_initial.py）
│   ├── tests/                   # pytest：config 首启 / DAO / 编排器调度与检查点
│   └── pyproject.toml
├── frontend/
│   └── src/
│       ├── pages/  ProjectsPage / ProjectDetailPage / RunDetailPage
│       ├── components/ StageList / TrajectoryTimeline / BlackboardViewer
│       ├── api/client.ts        # fetch 封装（后端 127.0.0.1:8000，vite proxy /api）
│       └── App.tsx / main.tsx   # react-router + AntD ConfigProvider（zhCN）
├── config/default.yaml
└── README.md
```

## 数据模型（Sprint 1 子集，字段与设计方案 §9 对齐）

| 表 | 关键字段 |
|---|---|
| `projects` | id, title, domain, goal, status(created/running/done), created_at, updated_at |
| `blackboard` | id, project_id, obj_type, version, payload(JSON), produced_by, evidence(JSON), created_at —— 同类型版本号自增 |
| `stage_checkpoints` | id, project_id, stage_id, status, snapshot(JSON), created_at —— 每阶段完成后写 |
| `agent_runs` | id, project_id, stage_id, agent_id, status(running/succeeded/failed), steps, started_at, finished_at |
| `agent_steps` | id, run_id, seq, kind(thought/tool/result/decision), content(JSON), created_at |

DAO 为纯函数模块（session 注入），不自持事务；API 层用 `with session.begin()`。

## 编排器设计（US-104）

- `StageRegistry`：`@register(stage_id="S1", agent_id="scout")` 装饰器注册；Sprint 1 注册 8 个占位阶段（`demo_stage.py`），每个 run 记录 1 条 thought + 1 条 result 步骤并写一个黑板对象
- `Orchestrator.run_stage(project_id, stage_id)`：
  1. 建 `agent_runs` 记录 → 2. 构造 StageContext → 3. 调用 agent.run()，每个动作写 `agent_steps` → 4. 黑板写入版本化对象 → 5. 成功后写 `stage_checkpoints` → 6. 更新 run 状态与步数
- 顺序调度：`Orchestrator.run_pipeline(project_id, stages=[...])` 按序执行，供后续联调；Sprint 1 前端仅单阶段触发
- 同步实现（SQLite 本地快），async JobWorker 移至 Sprint 2

## API（Sprint 1）

- `GET /api/health`
- `POST/GET /api/projects`、`GET/PATCH /api/projects/{id}`
- `POST /api/projects/{id}/stages/{stage_id}/run` → 返回 run_id（同步完成）
- `GET /api/projects/{id}/runs`、`GET /api/runs/{run_id}`（含 steps）、`GET /api/projects/{id}/blackboard`
- `GET /api/stages`（注册表元信息）

## 前端页面（US-105）

- **ProjectsPage**：项目列表 + 新建对话框（标题/领域/目标）
- **ProjectDetailPage**：阶段列表（8 个占位，带状态徽标）+「运行」按钮 + 黑板对象查看 + 该项目 run 列表
- **RunDetailPage**：轨迹回放 —— AntD Timeline 逐步展示 thought/tool/result，可展开 JSON 详情；「回放」按钮按 1s 步进高亮（纯前端定时器）
- api/client.ts 统一错误提示（AntD message）

## 实施顺序

1. `uv init` 后端骨架 + 配置加载 + 首启建目录（US-102）+ Alembic 初始迁移
2. ORM 模型 + DAO + pytest（US-103）
3. 编排器 + 占位阶段 + 阶段 API + 测试（US-104）
4. Vite + React + AntD 三页面 + 联调（US-105、US-101 收尾）
5. README 运行说明

## 验证

- `cd backend && uv run pytest` 全绿（config 首启、DAO CRUD、编排器调度/检查点/失败路径）
- 手动演示脚本：`uv run uvicorn app.main:app --reload` + `npm run dev` → 建项目 → 跑 S1 → RunDetailPage 看到完整轨迹且回放正常 → 黑板出现 `stage_output@v1`
- 复查：重启后项目与轨迹仍在（持久化）；`GET /api/health` 正常
