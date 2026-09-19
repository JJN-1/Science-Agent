# Sprint 2 实施计划：模型接入、记忆与治理（US-201 ~ US-206，36 SP）

> 批准于 Plan 模式评审。对应排期文档 Sprint 2（W3–W4）。

## 目标（对齐排期演示标准）
跑通一次带成本记账的真实 Agent 调用；故意触发预算熔断并走审批恢复；热切换模型后端不重启。

## 已确认的决策
- **Provider 范围**：OpenAI 兼容端点（httpx 直连，通吃 DeepSeek/智谱/硅基流动/OpenAI）+ **Mock Provider**（测试与零配置首启，默认配置指向 Mock，真实端点由用户在设置中配置 → 满足"未配置高档位不静默回落"）
- **API Key**：keyring 写 Windows 凭据管理器，config 只存 `api_key_ref` 引用名
- **审批 UX**：预算/步数熔断 → run 置 `paused` + 生成 `approvals` 行 → 前端会话流内嵌审批卡片，批准/拒绝后继续

## 架构增量（在 Sprint 1 代码之上）

### 1. 数据模型（Alembic migration 2 + `store/models.py` + DAO）
| 表 | 关键字段 |
|---|---|
| `agents` | agent_id(unique)、name、role、tier、tools(JSON)、budget_steps、budget_cost、enabled、config(JSON) |
| `decisions` | project_id、run_id?、stage_id、agent_id、kind(`decision`/`failed_attempt`)、decision、reason、alternatives(JSON)、decided_by |
| `llm_usage` | project_id?、run_id?、stage_id、agent_id、provider、model、tier、prompt_tokens、completion_tokens、cost、latency_ms、cached、degraded(JSON) —— 三维归因 |
| `approvals` | project_id、run_id、kind(`budget`/`step_limit`/`dangerous`)、detail(JSON)、status(`pending`/`approved`/`rejected`) |
| `provider_switch_log` | scope、old、new、source |

新 DAO：`store/dao/{agents,decisions,usage,approvals}.py`

### 2. `app/ai/` 模块（新包，对齐方案 §15 工程结构）
- **base.py**：`ChatRequest(messages, tier, schema?, max_tokens, temperature)`、`ChatResponse(text, prompt_tokens, completion_tokens, provider, model, degraded[], latency_ms)`、`ChatProvider` 协议（`name` / `capabilities: set` / `health()` / `complete()`）、异常族 `ProviderUnavailable / RateLimited / QuotaExceeded`
- **providers/mock.py**：确定性假模型（可配置延迟/失败/能力缺失，驱动熔断与降级测试）
- **providers/openai_compat.py**：chat/completions；超时 120s；指数退避+抖动重试 ≤3 次（§11.4）；能力探测（json_object / tools / stream）
- **registry.py**：`ProviderRegistry` —— 从 config 构建并 `health()` 检查（不健康不进注册表）；`reload()` 重建（US-206）写 `provider_switch_log`；`snapshot()` 供项目运行冻结；**熔断器**：单后端连续失败 5 次 → 冷却 5 分钟 → 走降级链
- **routing.py**：档位 `extract/plan/critique/synthesize/write` → 有序 `(provider, model)` 候选链；**配置加载期校验**：未知引用/能力缺失报错；`critique` 厂商 ≠ `plan`/`synthesize`（§8.2 约束）
- **degrade.py**：缺 JSON 模式 → schema 注入 system prompt + 失败携错重试 1 次；缺工具 → ReAct 文本协议占位；降级写 `ChatResponse.degraded` 并落库
- **budget.py**：项目总额 + 每日额度（config）；Agent 级步数/成本上限（agents 表）；**跨 Agent 缓存** `(provider, model, messages_hash, schema)` 命中零成本复用；`check_before_call()` 超限抛 `BudgetExceeded`
- **client.py**：`call_llm(session, project_id, run_id, stage_id, agent_id, tier, ...)` 唯一出口：预算检查 → 缓存 → 路由（熔断降级链）→ 调用 → 记 `llm_usage`（价格表按 config 每 1K token 计价）→ 写 `llm_call` 轨迹步骤

### 3. 编排层接线
- `StageContext` 增加 `llm(tier, messages, schema?)` 与 `decide(decision, reason, alternatives)`、`record_failure(...)`（US-204：决策与失败尝试自动落 `decisions`；run 失败时 Orchestrator 自动写 `failed_attempt`）
- `Orchestrator.run_stage` 捕获 `BudgetExceeded` → run 置 `paused` + 创建 `approvals` 行（不再计为 failed）
- 新增 `resume_stage(project_id, approval_id)`：批准后重跑该阶段

### 4. REST API（新路由 `api/settings.py`、`api/usage.py`、`api/decisions.py`、`api/approvals.py`）
- `GET/PATCH /api/settings/routing`（档位级热切换）
- `POST /api/settings/providers/reload`（全局热重载）+ `PUT /api/settings/providers/{name}/key`（写凭据管理器）
- `GET /api/settings/providers`（含 health 状态）
- `GET /api/usage/summary?project_id=`（按 stage / agent / provider 三维汇总）
- `GET /api/projects/{id}/decisions`
- `GET /api/approvals?status=pending` + `POST /api/approvals/{id}/approve|reject`

### 5. 前端（延续 Sprint 1 会话式界面）
- `RunBlock`：`llm_call` 步骤渲染 `⎿ llm plan · gpt-x · 1.2k tok · ¥0.003 · ⚠degraded`；run 头行显示累计成本；`paused` 状态显示 ⏸ + 流内审批卡片（批准/拒绝按钮 → POST 后刷新）
- 新增 `SettingsDialog`（侧栏齿轮打开）：Provider 健康列表、档位路由编辑、Key 录入（提交后端写凭据管理器）、热重载按钮
- `api/client.ts` 增加对应端点封装

### 6. demo 阶段升级（演示路径）
S1 scout 改为通过 `ctx.llm(tier="plan", schema=...)` 生成候选研究问题 JSON → 写 `research_questions@v1` 黑板对象（默认 Mock 后端保证零配置可演示；配置真实端点后即为真调用）

## 实施顺序（每步一个 commit）
0. **docs(Sprint 2)**：本计划落盘为工作区 `Sprint2_实施计划.md` 并提交
1. **feat(US-201/203 基座)**：migration 2 + models + 4 个 DAO + `app/ai` base/mock/registry/routing（配置校验 + 快照 + 熔断）+ 单测
2. **feat(US-202)**：openai_compat provider + keyring + degrade 降级链 + 单测
3. **feat(US-203/205)**：client.py 计费出口 + budget 缓存/熔断 + Orchestrator 暂停恢复 + approvals + 单测
4. **feat(US-204)**：decisions 自动/手动记录 + API + 单测
5. **feat(US-206)**：settings API（routing PATCH / reload / health / key）+ usage summary API + 单测
6. **feat(US-205/206 前端)**：审批卡片 + 成本展示 + SettingsDialog + client 封装
7. **docs(Sprint 2)**：README 更新 + Playwright 截图验证（含熔断审批卡片）+ S1 真实调用演示

## 依赖与约束
- 新增 Python 依赖（uv 项目内）：`keyring`；httpx 已有
- 全部测试用 Mock Provider，不依赖真实 Key；默认 config 路由全档位指向 Mock 并注释说明
- 验收：`uv run pytest` 全绿、`npm run build` + oxlint 通过、三个演示场景截图（带成本调用 / 熔断审批 / 热切换）
