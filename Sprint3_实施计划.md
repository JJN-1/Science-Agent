# Sprint 3 实施计划：技术债清偿 + 异步地基（US-301 ~ US-310，48 SP）

> 对应《修订排期与修复计划》§4，Sprint 3（W1–W2，2026-09-21 ~ 10-04）。
> 本冲刺**不产出任何面向用户的新能力**，只把 Sprint 1/2 补成真实可用。
> 这是全项目"含金量最低但必要"的一个冲刺——不做，Sprint 4 起每个冲刺都会踩它。

## 目标（对齐 G1 闸口）

Sprint 3 末必须通过 **G1 闸口**，三条通过标准：

1. 配一次真实模型 Key 能跑通 S1
2. 一个 30 秒以上的任务全程有实时进度
3. 预算批准后真能恢复运行

未通过则**不进入 Sprint 4**（S2 文献主线是分钟级任务，没有异步地基无法交付）。

---

## 起点：已完成 / 剩余

### 已完成（本次会话，7 个提交）

| FIX | 内容 | 提交 |
|---|---|---|
| FIX-06 | JSON 输出解析加固（围栏剥离 / 平衡提取 / 轻量修复 / 原始输出落轨迹） | `4de9110` |
| FIX-01 | `openai_compat` 注册进 `PROVIDER_TYPES` | `864ac17` |
| FIX-04 | provider 健康改三态 `ok/unconfigured/down`；不再按健康剔除 | `864ac17` |
| FIX-07 | `run_pipeline` 遇 paused 立即中断，后续阶段标 `skipped` | `a0ef1b6` |
| FIX-02 | `budget_grants` 表 + 有效限额 = 配置限额 + 未过期豁免 | `c418338` |
| — | 阶段失败时失败轨迹被事务回滚（探针发现的额外缺陷） | `fc8d934` |
| — | README 修复记录 + `.workbuddy/` 忽略 | `34d7f16` `2d8645a` |

对应故事：**US-301（部分）· US-302 · US-303 · US-306 已完成**。

### 剩余（本计划要做的）

| 故事 | FIX | 内容 | SP | 判定 |
|---|---|---|:--:|---|
| US-304 | FIX-03 | **run 由同步阻塞改为异步 job + SSE** | 15 | 🔴 核心，Sprint 4 硬前置 |
| US-305 | FIX-05 | 熔断状态与缓存持久化；缓存键改为实际命中的 provider | 5 | 🟡 |
| US-307 | FIX-08 | 占位阶段置灰 | 2 | 🟢 |
| US-308 | FIX-10 | CI（ruff + pytest / oxlint + tsc + build） | 3 | 🟢 |
| US-309 | FIX-11 | `docs/adr/` + 两条偏离记录 | 2 | 🟢 |
| US-310 | FIX-12 | 三类缺口测试补强（已过半） | 8 | 🟡 |
| US-301 | — | 真实模型 Key 跑通 S1 的走查与留证 | 3 | 🟡 Key 由用户测试时录入 |
| US-311 | — | 纵向切片走查：自由文本入口 → S1 → SSE 实时进度 → 结果落库 → 刷新仍在 → 成本与轨迹可见 | 2 | 🟡 |
| US-312 | — | 用户自定义模型接入：Provider 增删改 / 模型探测 API + 设置页接入表单 | 6 | 🔴 产品要求 |

**剩余 ≈ 46 SP。** FIX-12 已在本会话部分完成：配置文件→注册表→路由集成测试、Provider 契约负路径（401/429/超时/畸形 JSON）、编排状态机测试（暂停→批准→恢复不循环、pipeline 中断）均已补齐，测试数 46 → 81。剩余部分为**新增能力的回归测试**（job/SSE/缓存），随各步落地。

---

## 已确认的决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | **作业事件以数据库为准**（`job_events` 表），SSE 端点轮询该表增量投递，不做进程内 Queue 扇出 | 天然支持 `Last-Event-ID` 断线续传；跨进程/重启不丢事件；可用 `TestClient` 端到端测试。轮询间隔 ~200ms，对 SQLite 完全够用 |
| D2 | **阶段执行改为「增量提交」**：每个 step / 事件写库后立即 commit | SSE 读端是独立连接，SQLite WAL 下只有已提交数据可见——不增量提交就没有实时进度。且与已验证的「失败轨迹必须落库」语义一致 |
| D3 | `POST /run` 语义改为**受理即返回**（`{job_id, status:"queued"}`），不再返回 `run_id` | 直接对齐设计 §4.3「<100ms 返回 jobId + SSE」。旧同步路径不保留兼容开关，避免两套语义并存。**已确认：不保留 `?sync=true`** |
| D4 | 缓存的**键按每个候选分别试命中**，而不是只按首候选生成键 | 修掉真 bug：降级到 B 的响应曾被按 A 的键缓存，A 恢复后仍返回 B 结果直到重启 |
| D5 | 新建 `app_config` 表承载熔断状态 | 设计 §11.4 提到 `app_config`，但该表**实际从未创建**（只有 10 张业务表）——本次一并补上 |
| D6 | provider 网络探测沿用已有 30s TTL 缓存（FIX-04 时已加），本次只把熔断状态落库 | 避免重复设计 |

---

## 架构增量

### 1. 数据模型（migration 4，down_revision = `9f3c1a7b5d20`）

| 表 | 关键字段 | 用途 |
|---|---|---|
| `jobs` | id、project_id、kind(`stage`/`pipeline`)、stage_id、status(`queued`/`running`/`succeeded`/`failed`/`paused`)、run_id?、error?、params(JSON)、created_at、started_at、finished_at | 作业台账 |
| `job_events` | id、job_id、seq（job 内自增）、type、payload(JSON)、created_at | 事件日志，`seq` 即 SSE id |
| `app_config` | key(PK)、value(JSON)、updated_at | 熔断状态等运行期状态持久化 |
| `llm_cache` | cache_key(PK)、provider、model、tier、response(JSON)、created_at、expires_at | 跨重启缓存，TTL 默认 7 天（可配） |

新 DAO：`store/dao/jobs.py`（`create` / `get` / `list_for_project` / `add_event` / `events_after` / `next_seq` / `finish` / `recover_orphans`）、`store/dao/app_config.py`（`get` / `set`）、`store/dao/llm_cache.py`（`get` / `put` / `purge_expired`）。

### 2. 异步任务层（新包 `app/jobs/`）

- `app/jobs/runner.py` —— `JobRunner`：
  - `submit(session, project_id, kind, stage_ids|stage_id) -> job_id`：写 `jobs`（`queued`）+ `job.queued` 事件，`asyncio.create_task` 起后台协程
  - 后台协程用 `anyio.to_thread.run_sync` 跑同步编排（SQLAlchemy 同步引擎），**自建 session**（`app.state.session_factory`），不复用请求 session
  - 终态（succeeded / failed / paused）写 `jobs.status` + 对应事件；异常兜底绝不静默
  - `cancel(job_id)`：进程内 `asyncio.Task` 取消（尽力而为，不做跨进程）
- `app/jobs/events.py` —— `emit(session, job_id, type, payload)`：写 `job_events` 并 `commit`（D2）
- **启动自愈**：lifespan 里调 `jobs_dao.recover_orphans()`，把上次进程遗留的 `running` 任务标记为 `failed(error="进程中断")`，并补一条 `job.failed` 事件。避免僵尸 job 让前端永远转圈
- **并发**：进程内单 worker 队列（`asyncio.Queue` + 1 个消费协程），避免多个阶段同时抢 SQLite 写锁

### 3. 编排层接线（改造面：`StageContext` 是唯一收口）

- `StageContext` 增 `job_id: int | None = None`
- `StageContext.record()` 是 `think` / `decide` / 各 Agent 的公共出口 → 在写 `agent_steps` 后**镜像一条 `step` 事件**（`{kind, content, run_id}`），并 commit
- `LlmGateway.call(..., on_step: Callable | None = None)`：`_record` 之后回调一次，让 LLM 调用也实时可见。`StageContext.llm()` 传 `on_step=self._emit_llm_step`
- `Orchestrator.run_stage` 增可选 `job_id`，在阶段起止发 `stage.start` / `stage.succeeded` / `stage.paused` / `stage.failed`
- `Orchestrator.run_pipeline` 增可选 `job_id`，透传给每个阶段

### 4. REST / SSE API（`api/stages.py` 改造 + 新 `api/jobs.py`）

| 端点 | 变化 |
|---|---|
| `POST /api/projects/{id}/stages/{stage}/run` | **改为受理接口**：返回 `202 {job_id, status:"queued"}`；项目/阶段不存在仍 404 |
| `POST /api/projects/{id}/pipeline/run` | 新增：跑整条 pipeline，同样返回 `job_id` |
| `GET /api/jobs/{job_id}` | 新增：作业快照（供轮询回退） |
| `GET /api/jobs/{job_id}/stream` | 新增：**SSE**，`text/event-stream`；支持 `Last-Event-ID` 头与 `?after_seq=` 查询；deliver 到终态且事件取尽后正常关闭；每 15s 发 `: ping` 心跳 |
| `GET /api/projects/{id}/jobs` | 新增：项目作业列表（排查用） |

`api/stages.py` 里那段「失败先 commit 再抛 503」的特判逻辑**移入 worker**：worker 是失败现场的责任人，API 层不再承担。

### 5. 前端

- `api/client.ts`：`runStage` 改返回 `{job_id, status}`；新增 `getJob`、`listJobs`；新增 `streamJob(jobId, handlers)` —— 用原生 `EventSource`，**订阅失败回退轮询**（`EventSource.onerror` 连续 2 次 → 降级为 1s 轮询 `GET /api/jobs/{id}`）
- `SessionPage`：`handleRun` 不再 `await` 整个阶段；拿到 `job_id` 后打开流，把 `step` 事件追加进**流内实时缓冲**；收到终态事件（`job.succeeded/failed/paused`）后做一次完整 `load()` 并丢弃缓冲，保证最终状态以数据库为准
- `RunBlock`：支持渲染实时缓冲的步骤；头部显示 job 状态与已用时
- `CommandBar`：未实现阶段（D7）置灰 + tooltip

### 6. 阶段实现标记（FIX-08 / US-307）

- `StageAgent` 增 `implemented: bool = True`；`ScoutStage` 保持 `True`，`DemoStage` 置 `False`
- `demo_stage.STAGE_DEFS` 增计划交付冲刺（如 S2→Sprint 4），随 `StageOut` 下发
- `StageOut` 增 `implemented` / `planned_sprint` 字段（`api/schemas.py`）
- 前端 `CommandBar` 对 `implemented=false` 的阶段禁用并给出「占位实现，Sprint N 交付」提示

### 7. 熔断与缓存持久化（FIX-05 / US-305）

- `ProviderRegistry`：`_circuits` 读写改走 `app_config`（key = `circuit:{provider}`，value = `{failures, opened_at}`）。`record_failure` / `record_success` / `_is_open` 落库；`from_config` 时回读。**注意 `opened_at` 用 monotonic 的问题**：monotonic 跨进程无意义 → 改存 wall-clock（`time.time()`）时间戳
- `ProviderRegistry.reload()` 不再整体替换 `_circuits`，只对**已移除的 provider**清状态、新 provider 初始化，其余保留
- `LlmGateway`：缓存从进程内 `dict` 改走 `llm_cache` 表；命中时先查每个候选的键（D4）；写回时按**实际响应方**（`response.provider` / `response.model`）为键；`expires_at` 过期即失效并清理
- 配置项：`ai.cache.ttl_seconds`（默认 604800）、`ai.cache.enabled`（默认 true，测试可关）

### 8. CI 与 ADR（FIX-10 / FIX-11）

- `.github/workflows/ci.yml`：`backend` job（`uv sync` → `ruff check` → `pytest`）+ `frontend` job（`npm ci` → `oxlint` → `tsc -b` → `vite build`），两条并行，触发 `push` / `pull_request`
- `docs/adr/0000-template.md`、`0001-frontend-session-interface.md`（三页面 → 会话式单流）、`0002-tech-stack-react19-antd6.md`、
  `0003-async-job-and-sse.md`（本冲刺的异步地基决策，含 D1/D2/D3 的取舍）、`README.md` 索引
- 前端技术栈偏离需在 ADR 中说明「为何不是 React 18 + AntD 5」
- ADR-0004：对话层与结构化黑板的职责边界（对话层不持有研究状态）

### 9. 用户自定义模型接入（US-312）

对齐设计 §8.1 的「用户自定义 Provider 接入」。用户在设置页自行接入任意后端，不改代码、不重启。

**Provider 配置字段**（`config.yaml` 持久化 + `provider_switch_log` 审计）

| 字段 | 必填 | 说明 |
|---|:---:|---|
| `name` | ✅ | 唯一标识，被档位路由与 Agent 定义引用 |
| `type` | ✅ | `mock` / `openai_compat` / `anthropic_compat` |
| `base_url` | ✅ | 端点根地址；校验禁内网网段、禁跟随重定向 |
| `models` | ✅ | 可用模型 ID 列表 |
| `vendor` | ✅ | 厂商标识，供 `critique` 跨厂商校验 |
| `api_key_ref` | ⬜ | 凭据管理器引用名；本地端点可留空 |
| `capabilities` | ✅ | `json_object` / `tools` / `stream` / `vision`；未声明按缺失处理 |
| `price` | ⬜ | 每 1K token 输入 / 输出单价，用于成本归因 |
| `timeout_s` | ⬜ | 覆盖默认超时 |
| `extra_headers` / `extra_body` | ⬜ | 透传字段 |

**REST API**

| 端点 | 说明 |
|---|---|
| `GET /api/settings/providers` | 列出（含三态健康、能力、价格、被引用位置） |
| `POST /api/settings/providers` | 新增 |
| `PATCH /api/settings/providers/{name}` | 修改 |
| `DELETE /api/settings/providers/{name}` | 删除；被档位路由或 Agent 引用时拒绝并列出引用位置 |
| `PUT /api/settings/providers/{name}/key` | 录入 / 更新密钥（写凭据管理器） |
| `POST /api/settings/providers/{name}/probe-models` | 探测端点可用模型；不兼容 `GET {base_url}/models` 时返回提示要求手工填写 |
| `POST /api/settings/providers/reload` | 热重载全部 |

**关键行为**

- 新增或修改后**立即重建注册表**，并失效该 provider 的健康探测缓存，无需重启
- `capabilities` 与 `price` **只取本地配置**，不从上游模型清单推断（端点返回的清单只取 `id`）
- 协议一致性校验：`openai_compat` 的 provider 只允许挂载支持 Chat Completions 协议的模型
- 删除被引用 provider 时列出引用位置（档位名 / Agent id），不做级联删除

**前端**：设置对话框内的 Provider 列表（健康徽标）、接入表单（含「探测模型」按钮）、密钥录入、连通性测试按钮。

---

## 实施顺序（每步一个 commit）

| # | 提交信息 | 内容 | 验收 |
|---|---|---|---|
| 0 | `docs(Sprint 3): 实施计划落盘` | 本文件 | —— |
| 1 | `feat(US-307): 阶段实现标记与占位阶段置灰` | §6 全部 | `/api/stages` 返回 `implemented`；前端未实现阶段不可点且带 tooltip |
| 2 | `docs(US-309): ADR 目录与两条偏离记录` | §8 的 ADR 部分 | `docs/adr/` 可读、有索引 |
| 3 | `feat(US-308): 增加 CI 工作流` | §8 的 CI 部分 | 本地按工作流步骤手跑一遍全绿 |
| 4 | `feat(US-305): 熔断状态与 LLM 缓存持久化` | §7 + migration 4 的 `app_config`/`llm_cache` | 重启后熔断保留、缓存命中；降级恢复后不再返回旧缓存（回归测试） |
| 5 | `feat(US-312): 用户自定义模型接入` | Provider 增删改 + 模型探测 API + 设置页接入表单 + 路由引用校验 | 设置页填完即可用、无需重启；删除被引用 provider 时拒绝并列出引用位置；改 `base_url`/`capabilities` 后立即重建注册表并失效探测缓存 |
| 6 | `feat(US-304): 异步作业层与作业台账` | §1 的 `jobs`/`job_events` + §2 runner + §3 接线 | 单测：submit 立即返回、事件按 seq 可增量取、孤儿自愈 |
| 7 | `feat(US-304): run 改受理接口与 SSE 流` | §4 API | 单测：受理 P95 < 100ms（mock 下断言不阻塞）、SSE 端到端事件序列、`Last-Event-ID` 续传 |
| 8 | `feat(US-311): 纵向切片走查` | 自由文本入口改走 S1 + 真实 Key 走查 + 留证 | 自由文本能触发 S1；SSE 全程有进度；结果落库且刷新仍在；成本与轨迹可见 |
| 9 | `feat(US-304): 前端订阅作业流并实时渲染步骤` | §5 前端 | `npm run build` + oxlint 通过；长任务（mock 延迟）全程有进度 |
| 10 | `test(US-310): 补齐作业/SSE/持久化回归测试` | §7/§2/§4 的测试 + 纵向切片冒烟 | `uv run pytest` 全绿，测试数 ≥ 110 |
| 11 | `docs(Sprint 3): README 与 G1 闸口结论` | 结论留证 | G1 四条逐条留证 |

**顺序理由**：1–3 是零依赖的低风险项，先把"容易的收掉"；4 独立于 5–8；5→6→7→8 严格串行（数据模型 → API → 走查 → 前端）。

---

## 依赖与约束

- 无新增 Python 运行时依赖（SSE 用 `StreamingResponse`，任务用 `anyio`/`asyncio`，均为 FastAPI 自带）
- 测试仍全部走 Mock Provider；异步测试用 `TestClient` + 短轮询，不引入 `pytest-asyncio` 之外的新库（若需要则加 `pytest-asyncio`）
- **SQLite 写并发**：WAL + 单 worker 队列 + 请求侧仍然是 `get_session` 每请求一事务；worker 自己的 session 只由 worker 线程使用
- 前端不引入新的状态管理库，仍用 `useState` + 回调

## 验收清单（G1 闸口逐条）

| # | 标准 | 验证方式 | 状态 |
|---|---|---|---|
| G1-1 | 配真实 Key 能跑通 S1 | `tmp/probe_sprint3_real.py`：写入配置 → 启动 → run S1 → 产出 `research_questions` 且 `llm_usage` 有非 mock 记录 | ⬜ Key 由用户测试时录入 |
| G1-2 | 30 秒以上任务全程有实时进度 | Mock 延迟 30s 的 S1，前端观察步骤逐步出现；SSE 事件时间戳间隔可查 | ⬜ |
| G1-3 | 预算批准后真能恢复 | 已由 `c418338` 实现并验证（run 成功、0 条新 pending 审批），本次改为异步路径后**需重跑一遍** | ⬜ |
| — | 回归全绿 | `uv run pytest`（≥110）+ `npm run lint` + `npm run build` | ⬜ |
| — | 迁移安全 | 在真实库副本上 `upgrade head` 后行数不变（沿用上次的 `shutil.copy2` 绝对路径法） | ⬜ |

## 风险与裁剪预案

| 风险 | 影响 | 预案 |
|---|---|---|
| SSE 在 Windows/`uvicorn` 单进程下被缓冲，看不到流式 | G1-2 不达标 | 显式设 `X-Accel-Buffering: no`、`Cache-Control: no-cache`、`Connection: keep-alive`；开发用 `uvicorn --no-access-log` |
| SQLite 写锁竞争导致 `database is locked` | 阶段随机失败 | 单 worker 队列 + engine `timeout` 提升 + WAL 已开；必要时给写路径加进程内 `threading.Lock` |
| 增量提交破坏"阶段失败整体回滚"的原有预期 | 已有测试可能失败 | 这是**有意变更**（D2）；若有测试断言回滚语义，按新契约重写并在提交信息中说明，不改绿了事 |
| FIX-03 是 15 SP 的大件，做不完 | Sprint 4 无法开工 | 裁剪顺序：先砍 SSE 心跳与 `Last-Event-ID` 续传（保留基础流），再砍 pipeline 受理接口（保留单阶段）；**`jobs` 表与受理语义不可砍** |
| 真实 Key 拿不到 | G1-1 无法留证 | 用 openai_compat 对着任一兼容端点（如本地 ollama 的 OpenAI 兼容口）替代，仍能证明"真实 provider 路径打通"；结论中如实标注端点类型 |

## 已确认的决定

1. **真实模型 Key**：测试时由用户自行录入（经 US-312 的设置页接入表单 + 密钥录入）。US-301 / G1-1 在此之前保持待办，不阻塞其余步骤。
2. **`POST /run` 不保留 `?sync=true`**：直接改受理语义，前端与测试同步改，不留双语义通道。
3. **加 US-312（用户自定义模型接入）进 Sprint 3**：与 FIX-01 / FIX-04 / FIX-05 同批，避免重复改动 provider 注册表这一段代码。
4. **Sprint 3 总量 50 → 56 SP**；Sprint 4 起始时间不变。

---

*本计划按 §实施顺序 逐步落地，每步一个 commit，每步跑一次回归。*
