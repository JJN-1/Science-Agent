import type { AgentRun, AgentStep, JobStreamEvent } from './types'

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
    case 'stage.paused':
      return { live, done: false } // 紧随其后的 job.paused 才是终态
    case 'stage.failed':
      return {
        live: { ...live, steps: withoutPending(live.steps), error: String(payload.error ?? '阶段失败') },
        done: false,
      }
    case 'job.succeeded':
      return {
        live: { ...live, steps: withoutPending(live.steps), runId: runIdOf(payload, live.runId) },
        done: true,
      }
    case 'job.paused':
      return {
        live: {
          ...live,
          status: 'paused',
          steps: withoutPending(live.steps),
          runId: runIdOf(payload, live.runId),
        },
        done: true,
      }
    case 'job.failed':
      return {
        live: {
          ...live,
          status: 'failed',
          error: String(payload.error ?? '作业失败'),
          steps: withoutPending(live.steps),
          runId: runIdOf(payload, live.runId),
        },
        done: true,
      }
    case 'job.not_found':
      return {
        live: {
          ...live,
          status: 'failed',
          error: String(payload.error ?? '作业不存在'),
          steps: withoutPending(live.steps),
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
