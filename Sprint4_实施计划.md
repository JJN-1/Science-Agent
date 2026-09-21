# Sprint 4 实施计划：Agent 内核（US-401 ~ US-409，51 SP）

> 对应《修订排期与修复计划》§3.3 / §5.2 / §8.2，Sprint 4（W3–W4，2026-10-05 ~ 10-18）。
> 前三个冲刺做的是**地基**：数据模型、治理与记账、异步作业与 SSE、真实模型接入。
> Sprint 4 是第一次给这套系统装上**手**——能把「说」变成「做」，并把每一次动作
> 摆到人面前等着批准。
>
> 这也是阶段一的收尾冲刺：做完它，阶段二的 S1–S8 才能作为技能挂上来。
>
> **2026-09-20 修订**：初稿（48 SP / 8 步）把「模型能调工具」当成既有能力，代码核查显示
> **协议层一行都没有**。新增 **US-409 工具调用协议层**（3 SP）并前置，总计 **51 SP / 10 步**。
> 超出的 3 SP 由裁剪序列吸收。详见「开工前补丁」。

## 目标（对齐 G2 闸口）

Sprint 4 末必须通过 **G2 闸口**（内核验收）：

1. 3 个工具（`read_file` / `write_file` / `run_command`）完成一个 ≥5 步的真实任务
2. 全程流式可见（每一步都从 `job_events` 出来）
3. 执行命令前触发人工批准
4. 预算超限能暂停并恢复
5. 中断后能从检查点继续
6. `GET /api/tools` 返回全部工具及权限等级
7. `deterministic` 模式可用（同一输入两次跑出**相同步骤序列**，可断言）

未通过则**不进入 S5**：内核压缩为「工具调用协议层 + 工具注册表 + 调用循环 + 权限闸门」，
暂缓并行调用与上下文摘要化。**`deterministic` 不在裁剪序列内**——它是第 7 条的
唯一证据来源，而实现成本只是一个固定模板加三个固定量。

---

## 开工前补丁（2026-09-20 评估发现的三个缺口）

这份计划初稿把「模型能调工具」当成了既有能力。**代码级核查显示它不是**，
且三个缺口都是「不补就开不了工」或「不补就会长歪」的级别。

### 补丁 ①（P0，阻塞）：`tools` 协议层一行都没有 → 新增 US-409

| 实测 | 证据 |
|---|---|
| `ChatRequest` 字段只有 `messages / tier / schema / max_tokens / temperature / model`，**没有 `tools`** | `app/ai/base.py:15-23` |
| `ChatResult` 只有 `text / provider / model / tokens / latency / degraded / attempts`，**没有 `tool_calls`** | `app/ai/base.py:28-36` |
| `capabilities` 在三个文件里都声明了 `"tools"`，但全仓 `grep tool_choice\|function_call\|tool_calls` **只有声明、零消费点** | `base.py:108`、`providers/openai_compat.py:50`、`provider_config.py:25` |
| `agents.tools` 是死列，无任何读取点；设计 §5.3 `AgentSpec.tools` 从未落地 | `store/models.py:98`、`dao/agents.py:35` |

结论：**协议层无法表达一次工具调用**，注册表注册了也无处调用。US-409 因此**前置到第 3 步**，
排在工具注册表之前。

改动面（四文件 + 测试）：

- `app/ai/base.py`：`ChatRequest` 增 `tools: list[dict] | None` 与 `tool_choice: str | dict | None`；
  `ChatResult` 增 `tool_calls: list[ToolCall] | None`（`ToolCall = {id, name, arguments}`）
- `app/ai/providers/openai_compat.py`：请求体透传 `tools` / `tool_choice`；响应解析
  `choices[0].message.tool_calls`（含 `arguments` 是 **JSON 字符串**这一细节，需按
  `json_utils` 的容错路径解析）
- ⚠️ **`app/ai/degrade.py::_request_with` 手工重建 `ChatRequest`** —— 必须同步新字段，
  否则**降级重试时工具定义会被静默丢掉**（`model` 字段已经踩过这个坑，见项目记忆）
- `app/ai/providers/mock.py`：产出**确定性**工具调用，让内核测试可离线跑
- `app/ai/client.py`：缓存键要覆盖 `tools` 摘要 —— 否则「同一段对话带不同工具集」会串缓存

**SP 影响**：Sprint 4 由 48 → **51 SP**。超出的 3 SP 由裁剪序列吸收（并行调用为首选裁剪项），
不额外挤占排期。

### 补丁 ②（P0，会污染审计语义）：`tool.*` 事件与既有 `tool` 步骤种类撞名

前端的 `RunBlock.KIND_LABEL`（`components/RunBlock.tsx:5-10`）里，`tool` / `result` 已经是
**步骤种类**，含义是「Agent 自己写出来的文本步骤」——它们是模型叙述，不是系统执行。

本计划要新增的事件 `tool.call` / `tool.result` 指的是**系统真的执行了一次工具**。
两者若共用「tool」这个词，界面上就分不清「模型声称做了」和「系统真的做了」——
**而这恰恰是审计价值的全部来源**（D10 的「不静默撒谎」是同一个原则）。

**定名（D12）**：

| 概念 | 命名 | 来源 |
|---|---|---|
| 模型叙述的一个步骤 | 步骤种类 `note`（原 `tool` / `result` 保留读取兼容，写入侧一律用 `note`） | `agent_steps.kind` |
| 系统真实执行一次工具 | 事件 `tool.call` / `tool.result`；落 `tool_calls` 表 | `job_events` + `tool_calls` |

前端卡片标题也随之区分：`note` → 「步骤」，`tool.call` → 「工具调用（含权限徽标）」。

### 补丁 ③（P1，文档引用错位）：工具契约不在 §3.3

初稿写「工具契约（对齐设计 §3.3）」——**§3.3 实际是领域包的 `pack.yaml` 接口**。
工具契约在 **§5.3 `AgentSpec`**（`tools` / `reads` / `writes` / `max_steps` / `max_cost_usd` /
`requires_critic` / `human_checkpoint`）与 **§10.1 工具权限分级**。已在下文改正。

顺带记一笔：§5.3 那六个字段**至今一个都没实现**，它们是内核算法的真正输入——
`max_steps` / `max_cost_usd` 决定循环的停止条件，`reads` / `writes` 决定黑板权限，
`requires_critic` / `human_checkpoint` 决定评审与中断点。

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
| D12 | **「模型叙述的步骤」与「系统执行的工具」分开命名**：步骤种类用 `note`，事件用 `tool.call` / `tool.result` | 见补丁 ②。同名会让「声称做了」与「真的做了」在界面上不可分辨，审计价值归零 |
| D13 | **US-409（`tools` 协议层）前置到工具注册表之前**，且缓存键必须覆盖 `tools` 摘要 | 见补丁 ①。协议不通则注册表无处调用；缓存键不覆盖工具集会让不同工具组合串缓存 |
| D14 | **`POST /api/conversations/{id}/messages` 从第 1 步就定型为 `{message, job_id: int \| null}`**，第 1 步 `job_id` 恒为 `null`，内核算法就位后由第 5 步填上 | 避免第 5 步为了返回 `202 {job_id}` 而破坏已经联调过的响应契约。前端从一开始就按可空处理 |

---

## 架构增量

### 1. 数据模型（migration 5，`down_revision = b1c4e7a92d38`）

> 实际落地拆成三个迁移（原计划合成一个，按步拆开更好核对）：
> 5 = `3e7a5c91b4f2`（`conversations` / `messages`）、6 = `8b2f6c04d1e9`（`task_plans`）、
> **7 = `a7d3f8c21b64`（`tool_calls` 表 + `messages.tool_calls` 列）**。
> 加列**必须可空**：老 `messages` 行没有值可填，非空会当场升级失败。

| 表 | 关键字段 |
|---|---|
| `conversations` | id、project_id、title、status(`active`/`archived`)、created_at、updated_at |
| `messages` | id、conversation_id、role(`user`/`assistant`/`tool`/`system`)、content、tool_call_id、tool_calls(JSON, 可空)、tokens、created_at |
| `task_plans` | id、conversation_id、version、status(`draft`/`approved`/`executing`/`done`/`failed`)、mode(`plan_execute`/`react`)、steps(JSON)、created_at、updated_at |
| `tool_calls` | id、conversation_id、run_id、tool_name、args(JSON)、permission(`read`/`write`/`execute`/`dangerous`)、status(`ok`/`failed`/`rejected`)、result(JSON)、error、approval_id、duration_ms、created_at |
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

### 3. 工具契约（对齐设计 **§5.3 `AgentSpec`** 与 **§10.1 工具权限分级**）

> 初稿此处写「对齐设计 §3.3」有误——§3.3 是领域包的 `pack.yaml` 接口。工具契约的权威定义
> 在 §5.3（`tools` / `reads` / `writes` / `max_steps` / `max_cost_usd` / `requires_critic` /
> `human_checkpoint`）与 §10.1（权限分级、危险操作不可配置关闭）。

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

**第 4 步落地时的形状**（与上面同构，补上了缺省值并明确了失败通道）：

- `ToolSpec` 冻结，缺省 `permission="read"` / `idempotent=True` / `timeout_s=30.0` /
  `result_max_bytes=32768`（D10）。`__post_init__` 负责 trim 与校验：空名、空描述、
  未知权限、非正超时/上限一律抛 `AGENT-TOOL-001`
- `Tool.run(args, ctx) -> ToolResult`。`ToolContext` 携带 `session` / `project_id` /
  `job_id` / `agent_id` / `conversation_id`，**由内核注入**
- `ToolRegistry.invoke(name, args, ctx, *, allowed=None)` 是**唯一**触发工具副作用的入口：
  白名单 → 参数 schema → 执行 → 计时 → 截断（D10 头尾保留 + 写明丢了多少字节）
- `ToolSpec.to_tool_definition()` 产出 OpenAI 的 `tools` 元素（第 5 步交给 `ChatRequest.tools`）

**§5.3 的六个字段与本计划的对应关系**（它们是内核算法的真正输入，不能再悬空）：

| §5.3 字段 | 内核里的落点 | 本计划哪一步 |
|---|---|---|
| `tools` | 工具白名单收口（`AgentSpec.tools` ∩ 注册表） | 第 4 步（当前 `agents.tools` 是死列） |
| `reads` / `writes` | 黑板对象类型权限 | 第 6 步 |
| `max_steps` | 循环步数上限（`kernel.max_steps` 的 per-agent 覆盖） | 第 5 / 7 步 |
| `max_cost_usd` | 与 `BudgetManager` 双闸 | 第 7 步 |
| `requires_critic` | Critic 强制评审（阶段二 S8 接线） | 阶段二 |
| `human_checkpoint` | `none` / `before` / `after` / `risk_based` → 中断点 | 第 6 步 |

### 4. 调用循环

单轮：装配上下文 → 生成计划或下一步决策 → 有工具调用则执行 → 结果回填 → 下一轮。
上限 `kernel.max_steps`（默认 20）。同轮内无依赖的工具调用用 `anyio` 任务组并发，
**结果按调用序回填**（乱序回填会让模型的因果推断错位）。

**第 5 步落地时的形状**：

- `app/agent_kernel/loop.py`：`KernelRun`（坐标）/ `LoopOutcome`（战果）/
  `CallOutcome`（一次调用的结算，`key = (tool, args_hash(args))`）/ `KernelStore` 协议 /
  `KernelLoop`。入口 `run(session, *, run, store, plan=None, spec=None)`
- 模式由**生效中的计划**决定：有计划 → `plan_execute`（`tool_choice` 强制到该步声明的工具），
  没有 → `react`。`deterministic` 恒为 `plan_execute`，且 temperature 0 + 固定 seed + 禁并行（D7）
- `app/orchestration/kernel_store.py`：`SqlKernelStore` 实现协议；
  `open_chat_run()` 解析契约 / 建 run 行 / 装 store；`run_chat_job()` 是注入给 `JobRunner`
  的 `chat_handler`。可执行计划状态 = `approved` / `executing`（**`draft` 不在其中**：
  批准即冻结）
- `JobRunner.__init__` 收 `chat_handler`，`kind == "chat"` 走它；`main.py` 里接上
  `kernel_store.run_chat_job(app.state.kernel_loop, ...)`
- 新表 `tool_calls`（migration 7 `a7d3f8c21b64`）+ `messages.tool_calls` 列；
  `task_plans_dao.save_progress()` 与 `update_content()` **分开**（前者执行期只推状态、
  不加 `version`、不限状态；后者人工改内容、只在 `draft`、`version` 加一）

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
`POST /api/conversations/{id}/messages`、`GET /api/tools`、`PATCH /api/task-plans/{id}`。
复用：`GET /api/jobs/{id}`、`/stream`、`/cancel`。

`POST /api/conversations/{id}/messages` 的响应从第 1 步就定型为
`{ "message": MessageOut, "job_id": int | null }`（D14）：

- **第 1 步**：写用户消息、返回 `201`，`job_id` 恒为 `null` —— 内核还没就位，不假装派活
- **第 5 步**：内核循环接上后改为 `202`，`job_id` 为 `kind=chat` 作业的 id ✅

这样前端从第一天就按可空处理，第 5 步不需要回头改已经联调过的契约。

⚠️ 第 5 步落地时补的一条：**消息必须在受理作业之前显式 `commit`**。worker 是另一条线程、
另一个 session，它按 `job_id` 开跑时若读不到这条用户消息，本轮上下文里就没有用户刚说的
那句话 —— 表现为「模型答非所问」，且只在高频操作下偶发。依赖请求末尾那次自动提交是不够的：
入队发生在提交之前。

新增事件类型：`plan.updated`、`assistant.delta`、`tool.call`、`tool.result`、
`approval.required`。**沿用 Sprint 3 的帧约定：只有 `id:` + `data:`，类型在 `data.type`。**
命名与既有步骤种类的分工见 D12。

### 7. 前端

会话界面：消息流 + 计划卡片（步骤可编辑）+ 工具调用卡片（名称/参数/耗时/结果摘要/权限徽标）
+ 批准卡片；侧栏补工具清单与累计成本。SSE 订阅与轮询回退直接复用 `liveRun.ts` 的
`reduceLiveRun` 折叠模型，新增事件类型接进去即可。

---

## 实施顺序（10 步，每步一个 commit，每步跑一次回归）

> 原 8 步把「工具调用协议层」当作隐含前提，实测发现它一行都没有（补丁 ①）。
> 现拆为 US-409 并前置到注册表之前。**第 3 步与第 4 步不可合并**——
> 前者是协议改造（改的是 AI 接入层），后者是领域建模（改的是内核层），混在一个 commit 里
> 出问题时无法判断是协议映射错了还是注册表契约错了。

| # | 提交范围 | 内容 |
|:--:|---|---|
| 0 | `test(US-303)` | **前置**：G1-1 复跑留证（需用户录入真实 Key） |
| 1 | `feat(US-401/402)` | migration 5（`conversations`/`messages`）；会话 API（含 D14 契约）；上下文装配与裁剪 |
| 2 | `feat(US-403)` | `task_plans` 表与计划器；计划 API；确定性模式开关 |
| 3 | `feat(US-409)` | **工具调用协议层**：`ChatRequest.tools` / `ChatResult.tool_calls`；`openai_compat` 双向透传；`degrade` 同步；mock 确定性工具调用；缓存键覆盖 `tools` |
| 4 | `feat(US-404)` | 工具契约（**对齐设计 §5.3 `AgentSpec` / §10.1**）与注册表；**事件定名 `tool.call`/`tool.result`（D12）**；`GET /api/tools`；`run_pipeline` 注册为 capability |
| 5 | `feat(US-405)` | 内核循环（双模式、并行调用、结果截断、错误自愈）；`POST /messages` 改 `202`（D14） |
| 6 | `feat(US-406)` | 权限分级；最小沙箱；危险操作批准接线 |
| 7 | `feat(US-407)` | 执行控制：步数上限、预算熔断、中断、检查点恢复 |
| 8 | `feat(US-405)` 前端 | 前端：计划卡片、工具调用卡片、批准卡片、工具清单；`note` 与 `tool.call` 分卡渲染（D12） |
| 9 | `test(US-408)` | 内核端到端冒烟与回归；G2 留证 |

## 实施进度

| # | 提交范围 | 状态 | 留证 |
|:--:|---|:--:|---|
| 0 | `test(US-303)` G1-1 复跑 | ☐ 待用户录入真实 Key | — |
| 1 | `feat(US-401/402)` 会话持久化 + 上下文裁剪 | ✅ 已完成 | 回归 228 passed（基线 202，+26）；`tmp/migration_safety.py`（真实库副本 16 张表 193 行一字不差、新表幂等）；`tmp/smoke_sprint4.py`（真实 config.yaml 冒烟） |
| 2 | `feat(US-403)` 计划器 | ✅ 已完成 | 回归 **275 passed**（+47）；migration 6 加表安全（同两脚本，已泛化支持 `NEW_TABLES` 指定）；`react` 请求被明确拒绝而非静默降级 |
| 3 | `feat(US-409)` 工具调用协议层 | ✅ 已完成 | 回归 **308 passed**（+33）；`test_tool_protocol.py` 33 条，做过**变异检查**（去掉 `_request_with` 的字段同步与 `_serialize` 的 tool_calls → 7 条如实失败）；未进熔断的能力不匹配有独立异常类型 |
| 4 | `feat(US-404)` 工具注册表 | ✅ 已完成 | 回归 **366 passed**（+58）；新增 `test_tool_registry.py` / `test_agent_specs.py` / `test_tools_api.py` / DAO 三例；**变异检查 4/4 转红**（`tmp/mutation_us404.py`）；`tmp/migration_safety.py` 与 `tmp/smoke_sprint4.py`（真实 config.yaml + 真实库副本，脚本已加 US-404 段） |
| 5 | `feat(US-405)` 内核循环 | ✅ 已完成 | 回归 **407 passed**（+41）；`test_kernel_loop.py` 33 条（不碰数据库：`KernelStore` 协议 + 假件，逐条断言 `tool_choice` 强制、并行峰值、按调用序回填、`(tool, args_hash)` 计数）；**变异检查 4/4 转红**（`tmp/mutation_us405.py`，含未变异对照组自检）；`tmp/migration_safety.py` 新增「新增列」核对（16 张老表 193 行一字不差 / 4 张新表 / `messages.tool_calls` 列齐备 / 幂等）；`tmp/smoke_sprint4.py` 加 US-405 段（真实库副本上跑通 react 与 plan_execute 两条路） |
| 6 | `feat(US-406)` 权限与沙箱 | ☐ | — |
| 7 | `feat(US-407)` 执行控制 | ☐ | — |
| 8 | `feat` 前端会话界面 | ☐ | — |
| 9 | `test(US-408)` 内核冒烟 | ☐ | — |

**第 1 步的两处契约选择**（后续步骤不要改）：

- `POST /api/conversations/{id}/messages` 现在返回 `201 {message, job_id: null}`，
  `job_id` 是**可空字段**而不是「暂缺字段」—— 第 5 步只换值，不动形状（D14）
- `messages.tokens` 存**本地估算**，`llm_usage` 存上游**真实用量**。两者刻意不合并：
  合并之后就分不清哪个数字能信

**第 2 步的三处契约选择**（后续步骤不要改）：

- **模板与 `plan_execute` 绑定**：`react` 请求若只能走模板，**报 `AGENT-PLAN-002` 而不是
  降级成 `plan_execute`**。降级会产生一份「`mode` 写着 react、内容是固定八步」的计划 ——
  这正是 G2 第 8 条（模型声称与系统执行不得混淆）要防的东西。`react` 在第 5 步接入
- **`POST .../task-plans` 目前不接模型提案**：内核循环（第 5 步）才持有网关。缺省走编排
  模板，与「科研模式 = 替换计划模板」的衔接约定方向一致
- **确定性计划可改标题、不可改步骤**：执行只读 `steps`，改标题/说明不影响可复现；改步骤则
  「同一输入两次相同结果」不再成立。`revise_plan` 负责这条区分，接口层不重复实现

**第 3 步的四处契约选择**（后续步骤不要改）：

- **`arguments` 在协议层只搬运、不解析**：它在 OpenAI 协议里是 JSON 字符串。保持原样，
  解析交给 `ToolCall.parse_arguments()`；解析失败抛 `LLM-TOOLS-001` 而**不静默当空参数** ——
  把「参数看不懂」当成「没有参数」，工具会带着默认行为执行另一件事，而界面显示「成功」
- ⚠️ **`_request_with` 加字段必须同步**（它手工重建 `ChatRequest`）。已配一条
  `dataclasses.fields` 枚举式测试，以后新增字段漏同步会直接失败
- ⚠️ **`ChatMessage` 一同加了 `tool_call_id` 与 `tool_calls`**（超出原补丁清单，属必要补充）：
  没有它们就**无法把工具结果送回模型** —— 端点要求 `tool` 消息必须能对应上一条带
  `tool_calls` 的 `assistant` 消息，缺任一条会直接拒收整条请求。协议层一次做完整，
  第 5 步才不必回头再改（这正是把 US-409 前置的初衷）
- ⚠️ **能力不匹配用独立异常 `ToolCapabilityMissing`（`LLM-TOOLS-002`），降级链只跳过、
  不记失败**：否则「没配 tools 的后端被带工具的档位引用」这个**配置问题**会在 5 次调用后
  打开该后端熔断，连累它在**别的档位**上也不可用 —— 故障横向扩散到无关调用

**第 4 步的五处契约选择**（后续步骤不要改）：

- **`run_pipeline` 的编排函数是注入的**（`main.py` 传 `orchestrator.run_pipeline`），
  内核文件不 import `app.orchestration` —— 方向本来就是编排 → 内核，反向 import 成环。
  注入之后 `agent_kernel/tools/` 可以脱库、脱编排单测
- ⚠️ **`project_id` / `session` 由 `ToolContext` 注入，绝不从 `arguments` 取**。
  越权入口通常不是权限判断写错了，而是「这个值本来就不该由调用方给」：模型若能指定
  `project_id`，它就等于能跨项目读写
- ⚠️ **「工具失败」与「调用方违规」走两条通道**：工具自己失败 → `ToolResult(ok=False)`，
  模型看得见、能换参数重试（D9 的错误自愈计数就建立在这个字段上）；白名单 / 参数 schema /
  未注册 → 抛 `ToolError`（`AGENT-TOOL-001/002/003`），**在工具跑起来之前**，副作用为零。
  把后者也做成 `ok=False`，等于让模型去「修」一个它无权修改的白名单
- ⚠️ **`run_pipeline` 的 `stage_ids` 必填**：留空即「跑完全链路」这个默认值太贵
  （8 个阶段、真金白银），要求调用方显式列出。权限定 `execute` 而非 `dangerous` ——
  它不删数据也不出网；定成 `dangerous` 会让每次调用都要批准，把审批疲劳变成常态
- **`agents.tools` 的 `upsert` 语义是「`None` = 本次不动这个字段」**，且**必须更新已存在的行**：
  只更新插入路径的话，升级上来的安装永远拿不到白名单（新装的能用、老装的永远被拒）。
  注意判据用 `is not None` —— `budget_steps=0`（一步即熔断）是合法值
- **`AgentSpec` 不预造支撑 Agent（Critic / Curator / Steward / Human）的 spec**：
  它们还没有执行体，字段含义（评审阈值、记忆淘汰策略、审批边界）要等实现时才定得准

**第 5 步的八处契约选择**（后续步骤不要改）：

- ⚠️ **内核核心不 import `app.store`**：循环只认 `KernelStore` 协议（八条窄方法），
  实现是 `app/orchestration/kernel_store.py::SqlKernelStore` —— 唯一同时认识 DAO 与内核的一方。
  方向与第 4 步 `run_pipeline` 的注入一致（应用 → 内核）。换来的是循环的 **33 条单测一条
  都不需要数据库**：接了真实库之后，断言会退化成「跑完没报错」，而这里要钉的是
  中间那几步的形状
- ⚠️ **计划步骤声明了工具、模型却只回文本 → 那一步记 `skipped`，不是 `done`**
  （`_close_step`）。记 `done` 会让计划卡片显示「已完成」而实际什么都没发生 ——
  这是 D12「不把声称当执行」在内核内部的那一半：界面上分不出「工具真跑了」与
  「模型说自己跑了」，数据库就更不能替它混淆。跳过必须**带原因**写进 `plan.updated`
- ⚠️ **`called` 集合按步清零**，否则上一步调过的工具会替下一步「证明它跑过」——
  把「工具名出现过」当成「这一步执行了」，正是跳过检测唯一要区分的那件事
- ⚠️ **并行只对同轮多个 `read` 工具**，且每个调用借**独立 Session**（`Session` 不是线程安全的）；
  结果**按调用序**回填。乱序会让模型的因果推断错位：它看到结果 2 在结果 1 前面，
  只会假设自己的调用顺序与发起时不同，于是开始重排推理步骤去「解释」这个顺序
- ⚠️ **`plan.updated`（done）必须早于 `stage.succeeded` 发出**。按「收到终态即停止消费」
  实现的 SSE 读端会漏掉终态之后的任何一条 —— 表现为「跑完了但卡片还停在第一步」。
  **终态事件必须真的是最后一条**
- ⚠️ **预算熔断在循环内转成 `paused`，且计划回到 `approved`**（不是 `failed`）：复用 S3 的
  「暂停 → 审批 → 恢复」语义（D4）；留在 `executing` 会让重规划接口被 `has_running_plan`
  永远挡住，用户失去唯一的出路
- ⚠️ **`messages.tool_calls` 出到接口上**（`MessageOut`）：终态一律以数据库重拉为准，
  SSE 断了之后要重建这轮对话，助手那句「我来读一下」就得能指出它指的是哪次调用 ——
  否则后面那条 tool 消息挂着的 `tool_call_id` 找不到对手方
- **`CONVERSATION_SPEC`（`id="kernel"` / `stage="chat"`）单独播种成一行 agents**：
  `BudgetManager` 按 `agents.budget_steps` 判 Agent 级熔断，缺这一行，会话路径上
  只剩项目级闸门 ——「步数上限」在会话里会静默失效


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
| 4 | 预算超限可暂停并恢复 | 复用 S3 的预算熔断路径 + 内核循环下的等价用例 | ⚠️ 内核侧用例已就位（`test_budget_exceeded_pauses_instead_of_failing`、`test_budget_pause_returns_the_plan_to_approved`）；端到端恢复走第 7 步 |
| 5 | 中断可恢复 | 取消 → 从 `kernel_checkpoints` 续跑，步骤序号连续 | ☐ |
| 6 | `GET /api/tools` 返回全部工具及权限等级 | 接口快照 | ☐ |
| 7 | `deterministic` 模式可用 | **同一输入连跑两次，计划与步骤序列逐项相等**（第 9 步落成 golden case，见下方说明） | ☐ |
| 8 | 上下游不得在界面上混淆「模型声称」与「系统执行」 | 前端 `note` 与 `tool.call` 分卡渲染的截图（D12） | ☐ |

**第 7 条的留证标准不能只是「开关存在」**（2026-09-20 加严）：设计 §12.5 要求
「Golden Test：固定输入 + 固定断言；`temperature=0`；关键字段完全匹配」。内核层面的第一个
golden case 就落在第 7 条上——**同一输入跑两次，`task_plans.steps` 与 `tool_calls` 序列
逐项相等**。它同时也是 §12.5 在本项目的第一个可运行样本。

## 风险与裁剪预案

| 风险 | 预案 |
|---|---|
| Windows 沙箱做不彻底 | 第一版只做 D6 列出的可验证面；UI 明确标注执行真实性与边界，绝不声称「已隔离」 |
| 模型不按 JSON Schema 出工具调用 | 复用 `schema_utils` + 违规路径重试（FIX-06 已验证的机制） |
| 内核循环吃光预算 | `max_steps` + 熔断双闸；上下文裁剪优先保系统提示与当前计划 |
| **`tools` 协议层改动波及既有调用路径**（US-409） | 新字段一律**可选**（`None` 表示不带工具），既有 `ctx.llm()` 调用点零改动；`degrade._request_with` 的字段同步补一条**独立单测**，防止再次静默丢字段 |
| 进度落后（51 SP 偏大） | 裁剪顺序：**先砍并行调用 → 再砍上下文摘要化**；`deterministic` **不在裁剪序列内**（G2 第 7 条依赖它）。**工具调用协议层 + 工具注册表 + 调用循环 + 权限闸门必须保住** |
| 内核做完但不知质量如何 | 第 7 条升级为 golden case；阶段二 S5 起补「压缩存活」与「真实工具链」两类探针（本次评估列出的两项长期缺口） |
