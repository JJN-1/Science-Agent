# ResearchPilot · 科研全链路多 Agent 系统

覆盖科研全流程的本地多 Agent 协作系统：选题发现 → 文献综述 → 假设形式化 → 实验设计 → 执行采集 → 分析解读 → 写作成稿 → 投稿复现。

- 技术设计方案：`../科研全链路多Agent系统_技术设计方案.md`（唯一权威设计，代码里的 §编号指向它）
- 敏捷排期：`../科研全链路多Agent系统_敏捷冲刺排期.md`
- **当前排期（V2）**：`../修订排期与修复计划.md`
- Sprint 1 实施计划：`../Sprint1_实施计划.md`
- Sprint 2 实施计划：`../Sprint2_实施计划.md`
- Sprint 3 实施计划：`../Sprint3_实施计划.md`
- 架构决策记录：[`docs/adr/`](./docs/adr/README.md) · 走查留证：[`docs/evidence/`](./docs/evidence/)

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12+ · FastAPI · SQLAlchemy 2.0 · Alembic · SQLite（WAL） |
| 前端 | React 19 · TypeScript 6 · Vite 8 · Ant Design 6 · react-router 7 |
| AI 接入 | OpenAI 兼容端点（httpx）· keyring（凭据管理器存 Key） |
| 异步与流 | `asyncio.Queue` 单 worker + `anyio.to_thread` 跑同步编排 · SSE（`StreamingResponse`） |
| 工具 | uv（Python 依赖）· npm（前端依赖）· ruff / oxlint · pytest，全部项目内本地依赖 |

## 目录

```
researchpilot/
├── backend/            # FastAPI 服务（app/ + migrations/ + tests/）
│   ├── app/ai/         # AI 接入层：registry / routing / degrade / budget / client
│   ├── app/jobs/       # 异步作业层：runner / events（作业台账 + 事件流）
│   └── app/api/        # 路由：projects / stages / runs / jobs / governance / settings
├── frontend/           # React 前端（会话式单流界面 + src/components）
├── scripts/            # slice_walkthrough.py 等可复跑走查脚本
├── docs/adr/           # 架构决策记录
├── docs/evidence/      # 走查留证（含 G1 闸口结论）
└── config/default.yaml # 默认配置（用户配置在数据目录 config.yaml 覆盖）
```

## 快速开始

### 后端（端口 8000）

```bash
cd backend
python -m uv sync          # 创建 .venv 并安装依赖
python -m uv run pytest    # 运行测试（167 个用例）
python -m uv run ruff check .      # 后端 lint
python -m uv run uvicorn app.main:app --reload
```

首次启动会自动创建数据目录 `%APPDATA%\ResearchPilot\`（app.db / files / tex / workspace / packs / logs / config.yaml）并执行数据库迁移（US-102 零配置首启）。

### 前端（端口 5173，代理 /api → 8000）

```bash
cd frontend
npm install --include=dev  # 本机若设置了 omit=dev 必须加 --include=dev
npm run dev
npm run lint               # oxlint
npm run build              # tsc -b && vite build
```

访问 http://localhost:5173 ：左侧选项目 → 底部命令行**直接输入研究目标回车**（或 `run S1`）→ 转录流实时呈现 Agent 步骤（`⏺` / `⎿`），运行中块带 `job #N` 与走秒，命令行整条置灰。

### 可复跑走查

```bash
# 另开终端起后端后：
cd backend && uv run python ../scripts/slice_walkthrough.py --base http://127.0.0.1:8000
```

真实 HTTP 串一遍「自由文本 → S1 → SSE 进度 → 落库 → 刷新可见 → 成本与轨迹可见」，26 项检查，退出码 0 表示全过。

## 模型接入

**产品原则：用户自定义模型接入** —— 不绑定任何特定供应商或模型，端点在设置页自行增加。

- 默认零配置指向 **mock provider**，可完整演示档位路由、成本记账、预算熔断与审批
- 接真实模型：左侧「⚙ 设置 · 模型后端」→ 新增 provider（填 `name` / `type` / `base_url` / 可选模型）→「探测模型」→ 录入 API Key（写入 **Windows 凭据管理器**，不落盘明文）→ 把模型挂到档位路由 → 保存即**立即重建注册表并失效健康缓存**，无需重启，也不用点「热重载」
- **Provider 健康为三态**：`ok` / `unconfigured`（配置已写好但还没录 Key）/ `down`。未配置的后端不会导致启动失败，运行时会返回可操作提示
- 五档位路由：`extract` / `plan` / `critique` / `synthesize` / `write`，critique 档强制跨厂商（交叉验证约束）
- `capabilities` 与 `price` **只取本地配置**，不从上游模型清单推断（端点返回的清单只取 `id`）——所以任何不在标准清单里的模型也能接
- 删除被档位路由或 Agent 引用的 provider 会被**拒绝并列出引用位置**，不做级联删除
- 能力降级：缺 JSON 模式自动注入 schema 至 prompt，校验失败携错重试一次；全程记录 `degraded` 标记
- 输出解析容忍代码围栏、前后缀与尾随逗号；解析失败时把**原始输出**写入轨迹
- 预算治理：项目总额/每日额度 + Agent 级步数/成本上限；超限 run 自动**暂停**并在流内弹出审批卡片
- 审批生效方式：批准会签发一条带额度与有效期的**预算豁免**（`budget_grants`，默认 24h），有效限额 = 配置限额 + 未过期豁免，可在审批时覆盖额度

## 已实现

**Sprint 1（US-101 ~ 105）**：脚手架与零配置首启、五表数据模型与 DAO、Orchestrator（注册/调度/检查点）、轨迹查看与回放。

**Sprint 2（US-201 ~ 206）**：
- 多后端接入与档位路由（US-201）：`app/ai/` Provider 抽象 + Registry + Router
- 能力降级链（US-202）：schema 注入 / 携错重试 / 降级标记落库
- 成本记账与三维归因（US-203）：`llm_usage` 表按项目/阶段/Agent 汇总
- 决策与失败尝试日志（US-204）：`decisions` 表，Agent 手动 + 系统自动
- 预算熔断与审批（US-205）：暂停 + 流内审批卡片 + 批准后恢复
- 热重载与健康状态（US-206）：providers reload / routing PATCH / 切换审计

**Sprint 3（US-301 ~ 312）—— 异步作业 + 事件流 + 自定义模型 + G1 闸口**

- **异步作业层与作业台账（US-304 / FIX-03）**：`jobs` + `job_events` 两张表。`POST .../run` 改**受理语义**（立刻返回 `202 {job_id}`，受理耗时实测 **8ms 量级**：8.1ms / 8.3ms，不再是同步阻塞等阶段跑完），执行交给单 worker 的 `asyncio.Queue`，同步编排跑在 `anyio.to_thread` 上。事件全部落库、以 `seq` 为游标，**增量提交**（D2）——作业还在跑，事件已经被别的连接看得到
- **SSE 事件流**：`GET /api/jobs/{id}/stream`。帧形如 `id: <seq>` + `data: {"seq","type","payload"}`，**事件类型在 `data.type` 里而不是 SSE 的 `event:` 字段**（这样前端一个通用 `onmessage` 就能收，不必给每种类型注册监听器）。支持 `Last-Event-ID` / `after_seq` 续传（重连只补缺口）、空闲心跳、作业不存在时给 `job.not_found` 收尾帧
- **前端订阅并实时渲染步骤（US-304 §5）**：前端用浏览器原生 `EventSource` 订阅，把事件流**当成部分投影**渲染成一个独立的实时块（带 `job #N` 与 `已用时` 走秒、命令行整条置灰）；收到收尾帧就丢弃缓冲、**重新从数据库拉终态**（终态帧是 `job.succeeded/failed/paused`；订阅晚于终态时后端补播一次 `job.settled`）。连续 2 次订阅失败自动降级为 1s 轮询
- **自由文本入口（US-311）**：命令行不再只认 `run Sx`，直接输入研究目标即「先落 `goal` 再触发 S1」。顺序有语义 —— S1 构造提示词时读的就是 `project.goal`
- **用户自定义模型接入（US-312）**：Provider 增删改 + 模型探测 + 引用校验（见上节）
- **熔断状态与 LLM 缓存持久化（US-305）**：`app_config` / `llm_cache` 两张表，重启后熔断保留、缓存命中；降级恢复后不返回旧缓存
- **阶段实现标记（US-307）**：`/api/stages` 返回 `implemented`，前端未实现阶段不可点、带 tooltip
- **CI（US-308）**：`.github/workflows/ci.yml`，backend（ruff + pytest）与 frontend（oxlint + tsc + vite build）并行
- **ADR（US-309）**：`docs/adr/0001~0004`（会话式界面 / 技术栈 / 异步作业与 SSE / 对话层边界）
- **G1 闸口结论**：见 [`docs/evidence/G1-闸口结论.md`](./docs/evidence/G1-闸口结论.md)

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET/POST | `/api/projects` | 项目列表 / 创建 |
| GET/PATCH | `/api/projects/{id}` | 项目详情 / 更新（自由文本入口就是 PATCH `goal`） |
| GET | `/api/stages` | 阶段注册表（含 `implemented` / `planned_sprint`） |
| POST | `/api/projects/{id}/stages/{sid}/run` | **受理**一次阶段运行 → `202 {job_id, status}`，不再阻塞 |
| POST | `/api/projects/{id}/pipeline/run` | **受理**一次全链路运行（可选只跑指定阶段子集） |
| GET | `/api/jobs/{id}` | 作业台账（状态 / 指向的 run / 错误） |
| GET | `/api/jobs/{id}/events` | 事件回放（`?after_seq=` 只补缺口） |
| GET | `/api/jobs/{id}/stream` | **SSE 事件流**（`Last-Event-ID` 续传 + 空闲心跳） |
| POST | `/api/jobs/{id}/cancel` | 取消作业（仅 `queued`；执行中返回 409） |
| GET | `/api/projects/{id}/jobs` | 项目的作业列表 |
| GET | `/api/projects/{id}/runs` / `/api/runs/{id}` | 运行记录 / 轨迹明细 |
| GET | `/api/projects/{id}/blackboard` / `/checkpoints` | 黑板对象 / 检查点 |
| GET | `/api/projects/{id}/decisions` | 决策与失败尝试日志 |
| GET | `/api/projects/{id}/budget-grants` | 预算豁免审计（额度 / 审批单 / 有效期） |
| GET | `/api/usage/summary?project_id=&dim=` | 成本三维归因 |
| GET/POST | `/api/approvals` · `/api/approvals/{id}/approve|reject` | 审批流转 |
| GET/POST | `/api/settings/providers` | Provider 列表（三态健康 / 能力 / 价格 / 被引用位置）/ 新增 |
| PATCH/DELETE | `/api/settings/providers/{name}` | 修改 / 删除（被引用时拒绝并列出引用位置） |
| PUT | `/api/settings/providers/{name}/key` | Key 写入凭据管理器 |
| POST | `/api/settings/providers/{name}/probe-models` | 探测端点可用模型（不兼容 `GET /models` 时提示手工填写） |
| POST | `/api/settings/providers/reload` | 全局热重载 |
| GET | `/api/settings/provider-types` | 支持的 provider 类型 |
| GET/PATCH | `/api/settings/routing` | 档位路由查询 / 热切换 |

## 说明

- 数据可用环境变量 `RESEARCHPILOT_DATA_DIR` 重定向（测试使用）。
- 阶段 Agent：S1 已接入真实模型调用（plan 档结构化生成研究问题），S2–S8 为占位实现（`/api/stages` 返回 `implemented=false`，前端置灰），属阶段二范围，Sprint 5–11 逐个替换。
- **失败现场的第一责任人是 worker**：阶段失败时，`agent_runs.status=failed`、checkpoint、`failed_attempt` 决策、解析失败的原始输出**都会落库**，并通过 `job.failed` 事件推给前端会话流。受理接口（`POST .../run`）手上已经没有可失败的东西，所以它不再自己返回错误 —— 只回答「受理了没有」。
- 模型后端不可用这类错误会在 `job.failed` 事件的 payload 里带 `code`（如 `LLM-UNAVAIL-001` / `LLM-CIRCUIT-001`）与 `provider_error: true`，**可直接照做的修复指引在 `error` 里**（即 `str(exc)`），同样能在会话流里看到。`ProviderError` 的 `code` 遵循 `<域>-<类别>-<序号>` 约定。
- **事件类型在 `data.type` 里，不在 SSE 的 `event:` 字段**：这是有意的，前端一个通用 `onmessage` 就能收全量事件，不必给每种类型注册监听器（见 `docs/adr/0003`）。
- 作业状态（`succeeded` / `failed` / `paused`）与事件类型（`job.succeeded` / `job.failed` / `job.paused`）是**两个命名空间**，不要互相赋值 —— 混用会导致流永远不收敛。

## G1 闸口结论（Sprint 3 末）

**通过。** 逐条留证见 [`docs/evidence/G1-闸口结论.md`](./docs/evidence/G1-闸口结论.md)。

| 标准 | 状态 | 关键证据 |
|---|---|---|
| G1-1 配真实 Key 能跑通 S1 | ⬜ 待用户录入 Key | 链路已通到「真发请求」前一步；本机无凭据、也无本地兼容端点可替代 |
| G1-2 30 秒以上任务全程有实时进度 | ✅ | `delay_ms=30000` 实测 30070ms，**首帧 +29ms**（全程的 0.1%）；浏览器侧走查 8/8 |
| G1-3 预算批准后真能恢复（异步路径） | ✅ | `tests/test_budget_grants.py` 9/9，含「受理 → paused → 批准 → succeeded」整链 |
| 回归全绿 | ✅ | `pytest` **167 passed** · ruff clean · oxlint **0 error** · `tsc -b && vite build` 成功 |
| 迁移安全 | ✅ | `tests/test_migrations.py` 断言「只加表、老表行数不变 + 重复升级 no-op」；**真实库副本**（停在 Sprint 2 末 `246a42e41036`）连升 3 个迁移到 head，5 张新表出现、老表逐表行数一字不差；全新库冷启动迁移链一次跑通 |

遗留项（如实列出，不阻塞）：真实 Key 端到端、前端流降级路径的真机触发、断网重连的界面验证、取消按钮（等 Sprint 4 内核支持协作式取消）。

## 技术债修复记录

Sprint 3 之前已清的一批（**Sprint 3 前置**）：

| 编号 | 问题 | 处置 |
|---|---|---|
| FIX-01 | `openai_compat` 从未注册，配了真实模型应用直接起不来 | 注册该类型 + 补「配置 → 注册表 → 路由」集成测试 |
| FIX-02 | 批准预算后不产生任何豁免，形成「批准→再熔断」死循环 | 新增 `budget_grants` 表与有效限额计算，批准签发带额度/有效期的豁免 |
| FIX-04 | 未配 Key 被当作「不健康」静默剔除，报错指向错误方向 | `health()` 改三态，注册表不再剔除 provider，运行期给可操作提示 |
| FIX-06 | JSON 输出裸奔解析，代码围栏/前后缀直接判失败 | 抽出 `extract_json()`，解析失败保留原始输出 |
| FIX-07 | `run_pipeline` 遇 paused 不中断，堆出连环审批 | 暂停即中断，后续阶段标记 `skipped` |

Sprint 3 本批（**都是 Sprint 4 内核的硬前置**）：

| 编号 | 问题 | 处置 |
|---|---|---|
| FIX-03 | 阶段执行同步阻塞：分钟级任务撞 HTTP 超时，且全程看不到进度 | `jobs` / `job_events` 表 + 单 worker 队列；`POST /run` 改受理语义（`202 {job_id}`）；`GET /api/jobs/{id}/stream` SSE（含 `Last-Event-ID` 续传）；前端订阅 + 轮询回退 |
| FIX-05 | 熔断状态与 LLM 缓存只在内存里，重启即失忆；缓存键易串味 | 熔断落 `app_config`（`opened_at` 改 wall-clock，`reload()` 保留未变更 provider 的计数）；缓存改 `llm_cache` 表 + TTL（默认 7 天），**缓存键改用实际命中的 provider / model** |
| 附带 | 阶段失败时失败轨迹被事务回滚全部丢失 | 责任搬给 worker（`JobRunner._settle_failed`）：先用独立事务提交失败现场，再写 `job.failed` 事件 |
