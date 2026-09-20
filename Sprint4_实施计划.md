# Sprint 4 实施计划：Agent 内核（US-401 ~ US-408，48 SP）

> 对应《修订排期与修复计划》§3.3 / §5.2 / §8.2，Sprint 4（W3–W4，2026-10-05 ~ 10-18）。
> 前三个冲刺做的是**地基**：数据模型、治理与记账、异步作业与 SSE、真实模型接入。
> Sprint 4 是第一次给这套系统装上**手**——能把「说」变成「做」，并把每一次动作
> 摆到人面前等着批准。
>
> 这也是阶段一的收尾冲刺：做完它，阶段二的 S1–S8 才能作为技能挂上来。

## 目标（对齐 G2 闸口）

Sprint 4 末必须通过 **G2 闸口**（内核验收）：

1. 3 个工具（`read_file` / `write_file` / `run_command`）完成一个 ≥5 步的真实任务
2. 全程流式可见（每一步都从 `job_events` 出来）
3. 执行命令前触发人工批准
4. 预算超限能暂停并恢复
5. 中断后能从检查点继续
6. `GET /api/tools` 返回全部工具及权限等级
7. `deterministic` 模式可用

未通过则**不进入 S5**：内核压缩为「工具注册表 + 调用循环 + 权限闸门」，
暂缓并行调用、上下文摘要化与确定性模式（§7 裁剪表）。

---

## 起点：G1 闸口状态

| 条目 | 状态 | 证据 |
|---|---|---|
| G1-2 30s 以上任务全程有进度 | ✅ 已过 | `docs/evidence/US-311-纵向切片走查.md`；`scripts/slice_walkthrough.py` |
| G1-3 预算批准后真能恢复 | ✅ 已过 | `test_budget_grants.py`、`test_governance_api.py` |
| 回归与迁移安全 | ✅ 已过 | 202 passed；真实库副本逐表行数一字不差 |
| G1-4 纵向切片走查留证 | ✅ 已过 | 同 US-311 证据文档 |
| **G1-1 配一次真实 Key 跑通 S1** | ⚠️ **未闭环** | 见下 |

**G1-1 的实测结论（本轮，`tmp/smoke_g1_real_s1.py`）**：用真实配置的副本、关掉缓存、
走真实网络跑 S1，作业在 1.5s 内失败：

```
OpenRouter HTTP 401: {"error":{"message":"Missing Authentication header","code":401}}
```

根因不在链路：凭据管理器里 `OpenRouter` 那一条只有 **8 个字符**（真实 OpenRouter
Key 约 70 字符）。而 provider 健康仍显示 `ok` —— 因为健康探测打的是公开的
`GET /models`，该端点不需要鉴权，**配个假 Key 照样 200**。

本轮已补一条提示（`credential_view.key_suspicious` + 设置页「已录入·存疑」徽标）
把这个盲区摆到明面上，见提交 `426c5d1`。

**复跑方式**：在设置页重新录入真实 Key 后，`python tmp/smoke_g1_real_s1.py`
应打印「G1-1 通过」。

**为什么仍可开工**：内核的验收用**文件工具 + mock 模型**即可完成，不依赖真实 Key；
但 G2 要求「真实任务」，所以 G1-1 必须在 G2 之前闭环。**这条列为 Sprint 4 的
第 0 项前置**，由用户侧录入 Key 后复跑该脚本收口。

---

## 已确认的决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | **内核是「手」，编排层仍是「总指挥」**。`run_pipeline` 注册为内核工具暴露给模型，但阶段调度的状态机、回退边、检查点仍归 `Orchestrator` | 设计 §4.1 把内核层与编排层分成两层。把阶段调度复制一份进内核，等于让系统有两套「下一步做什么」的真相 |
| D2 | **对话层不持有研究状态**（衔接约定 4） | 研究状态始终在结构化黑板。对话只是入口，历史消息不是事实来源 |
| D3 | 内核循环**跑在现有 `JobRunner` 的 worker 里**，`POST /messages` 建一个 `kind=chat` 的 job，事件写 `job_events` | SSE 断线续传、`Last-Event-ID`、取消、僵尸作业自愈——这些已经验证过一遍。再开一条执行通道就是把它们重做一遍，还会分裂成两套 |
| D4 | 工具权限闸门**复用 `approvals` 表**（`kind=dangerous`），暂停语义复用 `job.paused` | Sprint 3 已把「暂停 → 审批 → 恢复」跑通并留证。内核另设一套人机协同入口会让用户看到两种审批卡 |
| D5 | **权限等级是工具契约的静态属性，不由模型决定**；`execute` 的「按目录记忆」记在 `app_config`（`tool_grant:<dir>`） | 让模型自称「这次是只读」就等于没有权限分级。记忆落库才能跨重启有效 |
| D6 | 沙箱第一版只做**可验证的最小面**：路径白名单、固定 cwd、超时、输出上限、默认禁网、产物 SHA256。**不做**受限令牌 / Job Object / 只读根文件系统 | 设计 §10.4 自己承认「Windows 上没有轻量容器，沙箱是真实技术难点」。**写清不做什么，比含糊地声称「有沙箱」更负责**；完整沙箱是 S9 的交付 |
| D7 | `deterministic=true` = 固定 `plan_execute` + 禁并行 + `temperature=0` + 固定 seed + 计划模板不许模型改写步骤顺序 | 用途是复现与评测（§12.4 消融），自由度就是噪声 |
| D8 | 上下文裁剪优先级固定：系统提示 > 当前计划 > 最近 N 轮 > 工具结果摘要 > 更早轮次（摘要化）；**摘要化本身的模型调用要计入预算** | 「更早轮次摘要化」是唯一会再花钱的一步，不计预算的省 token 会变成更贵 |
| D9 | 错误自愈计数按 **`(tool, args_hash)`** 计，连续 3 次失败终止该步并转人工 | 按工具名计数会误杀「换参数重试」——那本来就是正常操作 |
| D10 | 单工具结果超 32 KB 时**头尾保留 + 截断提示** | 与 FIX-06 同一条原则：静默截断等于对模型撒谎，它会在错误的前提下继续推理 |
| D11 | 无新增运行时依赖。并行用现有 `anyio`，token 估算用自写启发式 | 沿用 Sprint 3 约定；引 `tiktoken` 会让安装包与离线可用性都变差 |

---

## 架构增量

### 1. 数据模型（migration 5，`down_revision = b1c4e7a92d38`）

| 表 | 关键字段 |
|---|---|
| `conversations` | id、project_id、title、status(`active`/`archived`)、created_at、updated_at |
| `messages` | id、conversation_id、role(`user`/`assistant`/`tool`/`system`)、content、tool_call_id、tokens、created_at |
| `task_plans` | id、conversation_id、version、status(`draft`/`approved`/`executing`/`done`/`failed`)、mode(`plan_execute`/`react`)、steps(JSON)、created_at、updated_at |
| `tool_calls` | id、conversation_id、run_id、tool_name、args(JSON)、permission(`read`/`write`/`execute`/`dangerous`)、status、result(JSON)、error、approval_id、duration_ms、created_at |
| `kernel_checkpoints` | id、conversation_id、plan_id、step_index、status、snapshot(JSON)、created_at |

`task_plans.steps` 是**结构化对象**而非自由文本 —— 阶段二要能整体替换计划模板。

### 2. 新包 `app/agent_kernel/`

```
agent_kernel/
├── __init__.py
├── tools/
│   ├── base.py        # ToolSpec / ToolResult / Tool.run 契约、权限与幂等标注
│   ├── registry.py    # 注册表：schema 校验、按 name 查找、GET /api/tools 的数据源
│   ├── fs.py          # read_file / write_file / list_dir / glob / grep（白名单收口在 sandbox）
│   ├── shell.py       # run_command（cwd 锁定 + 超时 + 输出上限）
│   ├── web.py         # http_get（配置可关；默认关）
│   └── pipeline.py    # run_pipeline：把 S1–S8 确定性编排注册为 capability
├── sandbox.py         # 目录白名单解析、路径穿越防护、禁网、产物哈希
├── permissions.py     # 权限等级判定 + 批准闸门 + 按目录记忆
├── context.py         # token 预算装配与裁剪（优先级见 D8）
├── planner.py         # plan_execute 计划器 + 计划模板 + deterministic 开关
├── loop.py            # KernelLoop：双模式、并行调用、结果截断、错误自愈
├── checkpoints.py     # 每步落 kernel_checkpoints / 从中断处恢复
└── errors.py          # 内核错误码（沿用 §11.3 格式）
```

### 3. 工具契约（对齐设计 §3.3）

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict          # JSON Schema（复用 app/ai/schema_utils 校验）
    permission: str           # read | write | execute | dangerous
    idempotent: bool
    timeout_s: float
    result_max_bytes: int
```

### 4. 调用循环

单轮：装配上下文 → 生成计划或下一步决策 → 有工具调用则执行 → 结果回填 → 下一轮。
上限 `kernel.max_steps`（默认 20）。同轮内无依赖的工具调用用 `anyio` 任务组并发，
**结果按调用序回填**（乱序回填会让模型的因果推断错位）。

### 5. 权限闸门

| 等级 | 行为 |
|---|---|
| `read` | 直通，记 `tool_calls` |
| `write` | 直通，记录并落检查点 |
| `execute` | 首次人工批准；批准后按工作目录记忆，同目录免批 |
| `dangerous` | **每次**必须批准（删除、网络写、越出白名单） |

设计 §10.1 明确：危险操作强制事前审批**不可配置关闭**。

### 6. API 与 SSE（对齐设计 §3.4）

新增：`POST/GET /api/conversations`、`GET /api/conversations/{id}`、
`POST /api/conversations/{id}/messages`（返回 `202 {job_id}`）、`GET /api/tools`、
`PATCH /api/task-plans/{id}`。
复用：`GET /api/jobs/{id}`、`/stream`、`/cancel`。

新增事件类型：`plan.updated`、`assistant.delta`、`tool.call`、`tool.result`、
`approval.required`。**沿用 Sprint 3 的帧约定：只有 `id:` + `data:`，类型在 `data.type`。**

### 7. 前端

会话界面：消息流 + 计划卡片（步骤可编辑）+ 工具调用卡片（名称/参数/耗时/结果摘要/权限徽标）
+ 批准卡片；侧栏补工具清单与累计成本。SSE 订阅与轮询回退直接复用 `liveRun.ts` 的
`reduceLiveRun` 折叠模型，新增事件类型接进去即可。

---

## 实施顺序（每步一个 commit，每步跑一次回归）

| # | 提交范围 | 内容 |
|:--:|---|---|
| 0 | `test(US-303)` | **前置**：G1-1 复跑留证（需用户录入真实 Key） |
| 1 | `feat(US-401/402)` | migration 5（`conversations`/`messages`）；会话 API；上下文装配与裁剪 |
| 2 | `feat(US-403)` | `task_plans` 表与计划器；计划 API；确定性模式开关 |
| 3 | `feat(US-404)` | 工具契约与注册表；`GET /api/tools`；`run_pipeline` 注册为 capability |
| 4 | `feat(US-405)` | 内核循环（双模式、并行调用、结果截断、错误自愈） |
| 5 | `feat(US-406)` | 权限分级；最小沙箱；危险操作批准接线 |
| 6 | `feat(US-407)` | 执行控制：步数上限、预算熔断、中断、检查点恢复 |
| 7 | `feat(US-405)` | 前端：计划卡片、工具调用卡片、批准卡片、工具清单 |
| 8 | `test(US-408)` | 内核端到端冒烟与回归；G2 留证 |

## 依赖与约束

- **无新增运行时依赖**（Sprint 3 约定，D11）
- **迁移安全**：用真实库副本逐一比前后逐表行数（沿用 Sprint 3 套路）
- **事务边界**：内核循环里的写操作**自己 commit**，与 `JobRunner` 一致 ——
  SSE 读端是独立连接，SQLite WAL 下只有已提交的数据可见
- **预算**：`max_steps` 与 `BudgetManager` 双闸；熔断走 `job.paused` + 审批单

## 验收清单（G2 闸口逐条）

| # | 条目 | 留证方式 | 状态 |
|:--:|---|---|:--:|
| 1 | 3 工具 × ≥5 步真实任务跑通 | `scripts/kernel_walkthrough.py` + 输出存档 | ☐ |
| 2 | 全程流式可见 | 上述脚本收集的 `job_events` 序列（含 `tool.call`/`tool.result`） | ☐ |
| 3 | 危险操作可批准 | `run_command` 触发审批 → 批准 → 继续执行的事件留证 | ☐ |
| 4 | 预算超限可暂停并恢复 | 复用 S3 的预算熔断路径 + 内核循环下的等价用例 | ☐ |
| 5 | 中断可恢复 | 取消 → 从 `kernel_checkpoints` 续跑，步骤序号连续 | ☐ |
| 6 | `GET /api/tools` 返回全部工具及权限等级 | 接口快照 | ☐ |
| 7 | `deterministic` 模式可用 | 同一输入连跑两次，计划与步骤序列一致 | ☐ |

## 风险与裁剪预案

| 风险 | 预案 |
|---|---|
| Windows 沙箱做不彻底 | 第一版只做 D6 列出的可验证面；UI 明确标注执行真实性与边界，绝不声称「已隔离」 |
| 模型不按 JSON Schema 出工具调用 | 复用 `schema_utils` + 违规路径重试（FIX-06 已验证的机制） |
| 内核循环吃光预算 | `max_steps` + 熔断双闸；上下文裁剪优先保系统提示与当前计划 |
| 进度落后（48 SP 偏大） | 按 §7 裁剪顺序：先砍确定性模式 → 再砍上下文摘要化 → 再砍并行调用；**工具注册表 + 调用循环 + 权限闸门必须保住** |
