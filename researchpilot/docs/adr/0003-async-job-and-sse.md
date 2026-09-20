# ADR-0003: 阶段执行改为异步作业 + SSE 事件流

- 状态：已采纳
- 日期：2026-09-19
- 关联：设计文档 §4.3 / §9 / §11.4；US-304（FIX-03）；Sprint 3 决策 D1–D3

## 背景

Sprint 1/2 的 `POST /api/projects/{id}/stages/{stage}/run` 是同步接口：请求一直阻塞到阶段跑完才返回 `run_id`。
S1 选题发现只调用一次模型，尚可忍受；S2 文献综述起是分钟级任务，同步接口会遇到三重问题：
HTTP 连接可能被中间层超时切断、用户全程看不到进度、前端无法区分「在跑」与「卡死」。

现有进度通道只有「跑完之后一次性读 `agent_steps`」，本质是事后回放，不具备实时性。

## 决策

> 我们决定把阶段执行改为异步作业：`POST /run` 受理即返回 `{job_id, status:"queued"}`，
> 作业事件写入数据库，SSE 端点按 `seq` 增量投递；前端以 `EventSource` 订阅。

具体取舍：

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 事件通道 | 事件以 `job_events` 表为准，SSE 端点轮询该表增量投递；不做进程内 `Queue` 扇出 |
| D2 | 提交语义 | 阶段执行改为「增量提交」——每个 step / 事件写库后立即 `commit` |
| D3 | 接口语义 | `POST /run` 只作答「是否受理」，不再返回 `run_id`；不保留 `?sync=true` 兼容开关 |

## 理由

- **D1**：以表为准天然支持 `Last-Event-ID` 断线续传；跨进程、跨重启不丢事件；可用 `TestClient` 做端到端断言。轮询间隔约 200ms，SQLite 完全够用。
- **D2**：SSE 读端是独立连接，SQLite WAL 下只有已提交数据对它可见——不增量提交就没有实时进度。该语义与「阶段失败时失败轨迹必须落库」一致。
- **D3**：单一语义避免两套调用约定并存；同步路径保留会让前端与测试同时维护两条分支。

## 与设计文档的差异

无。本决策落实设计文档 §4.3「`<100ms` 返回 jobId + SSE 进度」的要求，
并把 §11.4 提到的 `app_config` 表补齐（此前从未创建）。

## 后果

### 正面

- 受理接口不再受阶段耗时影响，长任务不会撞 HTTP 超时。
- 进度实时可见，且刷新页面后可从已提交事件重建当前状态。
- 事件可回放，测试与排障都有确定性输入。

### 负面 / 代价

- 事件写入频率提高，SQLite 写放大上升，需要通过单 worker 串行化避免写锁争抢。
- 进程中断会留下 `running` 状态的僵尸作业，必须依赖启动自愈（lifespan 内 `recover_orphans`）兜底。
- `cancel` 只能取消**尚未开跑**的作业，跨进程取消不支持。

### 需要跟随的动作

- lifespan 中接入 `recover_orphans()`，把遗留的 `running` 作业标记为 `failed(error="进程中断")` 并补发 `job.failed` 事件。
- SSE 端点需发送心跳（`: ping`，15s）以穿过中间层空闲超时。
- 前端 `EventSource` 连续订阅失败 2 次后降级为 1s 轮询 `GET /api/jobs/{id}`。

## 实现补充（Sprint 3 落地时确定）

| 主题 | 结论 | 理由 |
|---|---|---|
| SSE 帧形状 | 只发 `id:` + `data:`，事件类型放在 `data.type` 里，**不发 `event:` 字段** | 一旦写了 `event:`，浏览器只会触发同名监听器，通用 `onmessage` 不再响应；前端需要的是一条「什么都能收到」的流 |
| `cancel` 的作用域 | 仅未开跑的作业（从队列摘除 + 落 `failed`） | 同步编排跑在 `anyio` 工作线程里，Python 无法安全中断它；硬取消只会留下「线程还在写、台账已判死」 |
| worker 并发度 | 单 worker 串行（`asyncio.Queue` + 1 个消费协程） | SQLite 只有一个写者，并行只会把 wait 时间换成 timeout 风险；要并行得先换库 |
| 执行线程 | `anyio.to_thread.run_sync` + 线程内自建 session | 编排层是同步 SQLAlchemy，与事件循环同线程会把整个服务卡死；请求 session 在受理那一刻就已结束 |
| 队列入队 | 跨线程一律 `call_soon_threadsafe` | `asyncio.Queue.put_nowait` 的唤醒绑定事件循环，跨线程直调只会留下一个无人通知的 future，作业永远停在 `queued` |
| 前端订阅 | 原生 `EventSource` + 实时缓冲（`LiveRun`），**终态事件到达即丢弃缓冲并整表重拉** | `Last-Event-ID` 续传与断线重连是 `EventSource` 的内建行为，自己实现等于把它们重写一遍还更不稳；而流是「增量、可能丢帧」的，轨迹必须「完整且一致」——两者职责分开，界面上的最终状态一律以数据库为准 |
| 流收尾事件 | 除三个终态事件外，`job.settled`（订阅晚于终态，后端补播现状）与 `job.not_found` 同样意味着「别再等了」，前端一并关流 | 只认 `job.succeeded/failed/paused` 会让收尾帧落进「未知类型」，连接凭 `onerror` 重连两轮才发现没事——白等两秒 |

**命名陷阱**：作业状态是 `succeeded/failed/paused`，事件类型是 `job.succeeded/job.failed/job.paused`。
两者字符串相近但不能互比 —— 曾因此在 SSE 终止条件里写出「永不退出」的流，靠
`after_seq` 超过末条事件的用例才发现。判断作业是否终态请用
`app.store.models.JOB_TERMINAL_STATUSES`，判断事件用 `app.jobs.events.TERMINAL_EVENT_TYPES`。
