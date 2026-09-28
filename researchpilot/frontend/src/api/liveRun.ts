import type { AgentRun, AgentStep, JobStreamEvent, PlanStep } from './types'

/**
 * 流内实时缓冲（US-304 §5）。
 *
 * 一个正在跑的作业在界面上投影成的「尚未落库」运行块。它只负责一件事：
 * 让进度看得见。**状态正确性不归它管** —— 终态事件一到，页面重拉一次完整
 * 快照并把缓冲丢掉，最终状态永远以数据库为准。
 *
 * 之所以需要这么一层，是因为作业事件是「增量 + 可能丢帧」的（SSE 断线走轮询
 * 续传、订阅可能晚于终态），而运行轨迹必须是「完整且一致」的。两者职责分开，
 * 谁也不用为对方的坑负责。
 */
export interface LiveRun {
  jobId: number
  stageId: string
  agentId: string
  runId: number | null
  status: 'running' | 'failed' | 'paused'
  steps: AgentStep[]
  error: string | null
  startedAt: string
  /** 流降级为轮询时置位，界面据此说明「为什么不再实时了」。 */
  degraded: boolean
  // ── 会话内核事件（US-405/406）：同样只做「看得见」，落库以重拉为准 ──
  /** ``plan.updated`` 投影的计划；与数据库 TaskPlan 的 steps 形状一致。 */
  plan: LivePlan | null
  /** ``tool.call`` / ``tool.result`` 按 ``call_id`` 配对后的调用卡。 */
  toolCalls: LiveToolCall[]
  /** ``approval.required`` 投影的待批准卡（落库后由 approvals 表接手）。 */
  approval: LiveApproval | null
  /** ``assistant.delta`` 的当前草稿（每轮整段替换；收尾由重拉接管）。 */
  assistantDraft: LiveAssistant | null
  /** ``job.paused`` 的原因：budget / cancelled / tool_permission。 */
  pauseReason: string | null
}

export interface LivePlan {
  planId: number
  mode: string
  status: string
  steps: PlanStep[]
  /** ``plan.updated`` 不带标题/依据：实时卡显示占位标题，落库卡才有全文 */
  title?: string
  rationale?: string
}

export interface LiveToolCall {
  callId: string
  tool: string
  args: Record<string, unknown>
  permission: string
  /** null = 还在执行（只收到 tool.call，没等到 tool.result） */
  ok: boolean | null
  status: string
  durationMs: number | null
  error: string | null
  resultPreview: string | null
  truncated: boolean
}

export interface LiveApproval {
  approvalId: number
  reason: string
  pending: {
    call_id: string
    tool: string
    args: Record<string, unknown>
    permission: string
    reason: string
    needs_approval: boolean
  }[]
}

export interface LiveAssistant {
  text: string
  provider: string
  model: string
}

export function newLiveRun(jobId: number, stageId: string, agentId = ''): LiveRun {
  return {
    jobId,
    stageId,
    agentId,
    runId: null,
    status: 'running',
    steps: [],
    error: null,
    startedAt: new Date().toISOString(),
    degraded: false,
    plan: null,
    toolCalls: [],
    approval: null,
    assistantDraft: null,
    pauseReason: null,
  }
}

export interface LiveRunResult {
  live: LiveRun
  /** 作业已结束：调用方应做一次完整 load() 并丢弃缓冲。 */
  done: boolean
}

function runIdOf(payload: Record<string, unknown>, fallback: number | null): number | null {
  return typeof payload.run_id === 'number' ? payload.run_id : fallback
}

/**
 * ``step`` 事件 → 轨迹步骤。
 *
 * ``id`` 取负的 ``seq``：既不会和库里自增 id 撞车，用作 React key 也稳定。
 */
function traceStep(event: JobStreamEvent, payload: Record<string, unknown>, runId: number): AgentStep {
  const content = payload.content
  return {
    id: -event.seq,
    run_id: runId,
    seq: event.seq,
    kind: typeof payload.kind === 'string' ? payload.kind : 'thought',
    content:
      typeof content === 'object' && content !== null
        ? (content as Record<string, unknown>)
        : {},
    created_at: new Date().toISOString(),
  }
}

/**
 * ``llm.call`` 事件 → 轨迹步骤。
 *
 * 字段名刻意与后端 ``agent_steps`` 里 llm_call 的 content 对齐，
 * 这样同一个 RunBlock 不用为「实时」和「历史」写两套渲染。
 */
function llmStep(event: JobStreamEvent, payload: Record<string, unknown>, runId: number): AgentStep {
  return {
    id: -event.seq,
    run_id: runId,
    seq: event.seq,
    kind: 'llm_call',
    content: {
      provider: payload.provider,
      model: payload.model,
      tier: payload.tier,
      prompt_tokens: payload.prompt_tokens,
      completion_tokens: payload.completion_tokens,
      cost: payload.cost,
      latency_ms: payload.latency_ms,
      attempts: payload.attempts,
      cached: payload.cached,
      degraded: payload.degraded,
    },
    created_at: new Date().toISOString(),
  }
}

/**
 * ``llm.start`` 事件 → 等待占位步骤。
 *
 * 模型调用是全链路最慢的一步，而 ``llm.call`` 要等它**结束**才发得出来 ——
 * 只靠结算事件，等待期在界面上就是一片死寂（真实事故：「点了运行，好几分钟没反应」）。
 * 占位步骤只活在流里、不落库：它表达的是「正在等」，不是「等过」。
 */
function llmStartStep(event: JobStreamEvent, payload: Record<string, unknown>, runId: number): AgentStep {
  return {
    id: -event.seq,
    run_id: runId,
    seq: event.seq,
    kind: 'llm_pending',
    content: {
      tier: payload.tier,
      chain: Array.isArray(payload.chain) ? payload.chain : [],
    },
    created_at: new Date().toISOString(),
  }
}

/** 摘掉等待占位：它只在「等待中」这一瞬间有意义。 */
function withoutPending(steps: AgentStep[]): AgentStep[] {
  return steps.filter((s) => s.kind !== 'llm_pending')
}

// ── 会话内核事件（US-405/406）────────────────────

function planOf(payload: Record<string, unknown>, fallback: LivePlan | null): LivePlan {
  const steps = Array.isArray(payload.steps) ? (payload.steps as PlanStep[]) : []
  return {
    planId: typeof payload.plan_id === 'number' ? payload.plan_id : (fallback?.planId ?? -1),
    mode: typeof payload.mode === 'string' ? payload.mode : (fallback?.mode ?? ''),
    status: typeof payload.status === 'string' ? payload.status : (fallback?.status ?? ''),
    steps,
  }
}

function upsertToolCall(calls: LiveToolCall[], next: LiveToolCall): LiveToolCall[] {
  const idx = calls.findIndex((c) => c.callId === next.callId)
  if (idx === -1) return [...calls, next]
  const merged = { ...calls[idx], ...next, args: next.args ?? calls[idx].args }
  return [...calls.slice(0, idx), merged, ...calls.slice(idx + 1)]
}

/**
 * 终态前的收尾：摘掉等待占位、丢掉草稿。计划的最终状态、工具结果、
 * 助手消息都会由收尾重拉从数据库接手，缓冲里留着只会出现两份。
 */
function withTerminalShape(live: LiveRun): LiveRun {
  return { ...live, steps: withoutPending(live.steps), assistantDraft: null }
}

/** 把一帧事件叠进实时缓冲。纯函数，便于单测与在 StrictMode 下重放。 */
export function reduceLiveRun(live: LiveRun, event: JobStreamEvent): LiveRunResult {
  const payload = event.payload ?? {}
  // 订阅晚于终态时 SSE / 轮询会补发 job.settled，它的 status 就是作业的终态，
  // 拍成普通终态事件即可复用后面的分支。
  const type =
    event.type === 'job.settled' ? `job.${String(payload.status ?? 'succeeded')}` : event.type

  switch (type) {
    case 'stage.start':
      return {
        live: {
          ...live,
          stageId: typeof payload.stage_id === 'string' ? payload.stage_id : live.stageId,
          agentId: typeof payload.agent_id === 'string' ? payload.agent_id : live.agentId,
          runId: runIdOf(payload, live.runId),
        },
        done: false,
      }
    case 'step': {
      const runId = runIdOf(payload, live.runId) ?? -1
      return {
        live: { ...live, runId, steps: [...live.steps, traceStep(event, payload, runId)] },
        done: false,
      }
    }
    case 'llm.start': {
      const runId = runIdOf(payload, live.runId) ?? -1
      return {
        live: {
          ...live,
          runId,
          steps: [...withoutPending(live.steps), llmStartStep(event, payload, runId)],
        },
        done: false,
      }
    }
    case 'llm.call': {
      const runId = runIdOf(payload, live.runId) ?? -1
      return {
        live: {
          ...live,
          runId,
          steps: [...withoutPending(live.steps), llmStep(event, payload, runId)],
        },
        done: false,
      }
    }
    case 'plan.updated':
      return { live: { ...live, plan: planOf(payload, live.plan) }, done: false }
    case 'tool.call': {
      const callId = String(payload.call_id ?? '')
      if (!callId) return { live, done: false }
      const next: LiveToolCall = {
        callId,
        tool: String(payload.tool ?? ''),
        args:
          typeof payload.args === 'object' && payload.args !== null
            ? (payload.args as Record<string, unknown>)
            : {},
        permission: String(payload.permission ?? ''),
        ok: null,
        status: 'running',
        durationMs: null,
        error: null,
        resultPreview: null,
        truncated: false,
      }
      return { live: { ...live, toolCalls: upsertToolCall(live.toolCalls, next) }, done: false }
    }
    case 'tool.result': {
      const callId = String(payload.call_id ?? '')
      if (!callId) return { live, done: false }
      const existing = live.toolCalls.find((c) => c.callId === callId)
      // 只收到 tool.result 而没见过对应 tool.call（订阅晚了、帧丢了）也照常成卡：
      // 结果本身是完整的，args 之类补不上就留空，总比整张卡消失好。
      const next: LiveToolCall = {
        callId,
        tool: String(payload.tool ?? existing?.tool ?? ''),
        args: existing?.args ?? {},
        permission: existing?.permission ?? '',
        ok: payload.ok === true,
        status: String(payload.status ?? ''),
        durationMs: typeof payload.duration_ms === 'number' ? payload.duration_ms : null,
        error: typeof payload.error === 'string' ? payload.error : null,
        resultPreview:
          typeof payload.result_preview === 'string' ? payload.result_preview : null,
        truncated: payload.truncated === true,
      }
      return { live: { ...live, toolCalls: upsertToolCall(live.toolCalls, next) }, done: false }
    }
    case 'assistant.delta':
      return {
        live: {
          ...live,
          assistantDraft: {
            text: String(payload.text ?? ''),
            provider: String(payload.provider ?? ''),
            model: String(payload.model ?? ''),
          },
        },
        done: false,
      }
    case 'approval.required':
      return {
        live: {
          ...live,
          approval: {
            approvalId: typeof payload.approval_id === 'number' ? payload.approval_id : -1,
            reason: String(payload.reason ?? ''),
            pending: Array.isArray(payload.pending)
              ? (payload.pending as LiveApproval['pending'])
              : [],
          },
        },
        done: false,
      }
    case 'stage.paused': {
      // 会话作业的暂停事件带 reason（budget / cancelled / tool_permission），
      // 先记下来：紧随其后的 job.paused 里没有这个字段。
      return {
        live: {
          ...live,
          pauseReason: typeof payload.reason === 'string' ? payload.reason : live.pauseReason,
        },
        done: false,
      }
    }
    case 'stage.failed':
      return {
        live: { ...withTerminalShape(live), error: String(payload.error ?? '阶段失败') },
        done: false,
      }
    case 'job.succeeded':
      return {
        live: {
          ...withTerminalShape(live),
          runId: runIdOf(payload, live.runId),
        },
        done: true,
      }
    case 'job.paused':
      return {
        live: {
          ...withTerminalShape(live),
          status: 'paused',
          runId: runIdOf(payload, live.runId),
        },
        done: true,
      }
    case 'job.failed':
      return {
        live: {
          ...withTerminalShape(live),
          status: 'failed',
          error: String(payload.error ?? '作业失败'),
          runId: runIdOf(payload, live.runId),
        },
        done: true,
      }
    case 'job.not_found':
      return {
        live: {
          ...withTerminalShape(live),
          status: 'failed',
          error: String(payload.error ?? '作业不存在'),
        },
        done: true,
      }
    default:
      return { live, done: false }
  }
}

/** 缓冲 → RunBlock 需要的 AgentRun 形状。 */
export function liveRunToAgentRun(live: LiveRun, projectId: number): AgentRun {
  return {
    id: live.runId ?? -1,
    project_id: projectId,
    stage_id: live.stageId,
    agent_id: live.agentId,
    status: live.status,
    steps: withoutPending(live.steps).length,
    error: live.error,
    started_at: live.startedAt,
    finished_at: null,
  }
}
