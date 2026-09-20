import type {
  AgentRun,
  Approval,
  BlackboardItem,
  Decision,
  Job,
  JobEvent,
  JobStreamEvent,
  ProbeResult,
  Project,
  ProviderHealth,
  ProviderInput,
  ProviderTypes,
  RunDetail,
  StageInfo,
  TierRoute,
  UsageSummary,
} from './types'

const BASE = '/api'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    let detail: unknown = resp.statusText
    try {
      const body = await resp.json()
      detail = body.detail ?? detail
    } catch {
      // 非 JSON 响应，保留 statusText
    }
    throw new ApiError(
      resp.status,
      typeof detail === 'string' ? detail : JSON.stringify(detail),
    )
  }
  return resp.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string }>('/health'),
  listProjects: () => request<Project[]>('/projects'),
  createProject: (data: { title: string; domain: string; goal: string }) =>
    request<Project>('/projects', { method: 'POST', body: JSON.stringify(data) }),
  getProject: (id: number) => request<Project>(`/projects/${id}`),
  updateProject: (id: number, data: { goal?: string; title?: string; status?: string }) =>
    request<Project>(`/projects/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  listStages: () => request<StageInfo[]>('/stages'),
  /** FIX-03：受理接口只承诺「已排队」，执行结果从作业流上读。 */
  runStage: (projectId: number, stageId: string) =>
    request<{ job_id: number; status: string }>(
      `/projects/${projectId}/stages/${stageId}/run`,
      { method: 'POST' },
    ),
  runPipeline: (projectId: number, stageIds?: string[]) =>
    request<{ job_id: number; status: string }>(`/projects/${projectId}/pipeline/run`, {
      method: 'POST',
      body: JSON.stringify(stageIds ? { stage_ids: stageIds } : {}),
    }),
  getJob: (jobId: number) => request<Job>(`/jobs/${jobId}`),
  listJobs: (projectId: number) => request<Job[]>(`/projects/${projectId}/jobs`),
  getJobEvents: (jobId: number, afterSeq = 0) =>
    request<JobEvent[]>(`/jobs/${jobId}/events?after_seq=${afterSeq}`),
  cancelJob: (jobId: number) =>
    request<{ job_id: number; status: string; cancelled: boolean }>(
      `/jobs/${jobId}/cancel`,
      { method: 'POST' },
    ),
  listRuns: (projectId: number) => request<AgentRun[]>(`/projects/${projectId}/runs`),
  getRun: (runId: number) => request<RunDetail>(`/runs/${runId}`),
  getBlackboard: (projectId: number) =>
    request<BlackboardItem[]>(`/projects/${projectId}/blackboard`),
  // ── Sprint 2：治理与设置 ──
  listDecisions: (projectId: number) =>
    request<Decision[]>(`/projects/${projectId}/decisions`),
  usageSummary: (projectId: number, dim: 'agent' | 'stage' | 'provider') =>
    request<UsageSummary>(`/usage/summary?project_id=${projectId}&dim=${dim}`),
  listApprovals: (projectId: number, status = 'pending') =>
    request<Approval[]>(`/approvals?project_id=${projectId}&status=${status}`),
  decideApproval: (approvalId: number, action: 'approve' | 'reject') =>
    request<{ approval_id: number; status: string; new_run_id?: number | null }>(
      `/approvals/${approvalId}/${action}`,
      { method: 'POST', body: JSON.stringify({ note: '' }) },
    ),
  getProviders: () => request<ProviderHealth[]>('/settings/providers'),
  createProvider: (data: ProviderInput) =>
    request<{ provider: string; config: ProviderInput }>('/settings/providers', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateProvider: (name: string, data: ProviderInput) =>
    request<{ provider: string; config: ProviderInput }>(
      `/settings/providers/${name}`,
      { method: 'PATCH', body: JSON.stringify(data) },
    ),
  deleteProvider: (name: string) =>
    request<{ provider: string; removed: boolean }>(`/settings/providers/${name}`, {
      method: 'DELETE',
    }),
  probeModels: (name: string, data: { base_url?: string; api_key?: string } = {}) =>
    request<ProbeResult>(`/settings/providers/${name}/probe-models`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  getProviderTypes: () => request<ProviderTypes>('/settings/provider-types'),
  reloadProviders: () =>
    request<{ providers: string[]; removed: string[]; added: string[] }>(
      '/settings/providers/reload',
      { method: 'POST' },
    ),
  getRouting: () => request<Record<string, TierRoute[]>>('/settings/routing'),
  patchRouting: (tier: string, candidates: TierRoute[]) =>
    request<{ tier: string; candidates: TierRoute[] }>('/settings/routing', {
      method: 'PATCH',
      body: JSON.stringify({ tier, candidates }),
    }),
  setProviderKey: (name: string, key: string) =>
    request<{ provider: string; stored: boolean }>(`/settings/providers/${name}/key`, {
      method: 'PUT',
      body: JSON.stringify({ key }),
    }),
}

// ── 作业事件流订阅（US-304 §5）────────────────────

/**
 * 收到其中之一即表示「这条流没有后续了」，可以收工。
 *
 * 除了三个终态事件，还有两种收尾帧：``job.settled``（订阅晚于终态，后端补播
 * 一次现状）与 ``job.not_found``。它们同样意味着「别再等了」。
 */
export const STREAM_END_EVENT_TYPES = [
  'job.succeeded',
  'job.failed',
  'job.paused',
  'job.settled',
  'job.not_found',
]

export const TERMINAL_JOB_STATUSES = ['succeeded', 'failed', 'paused']

/** SSE 连续失败几次后降级为轮询。 */
const SSE_MAX_FAILURES = 2
const POLL_INTERVAL_MS = 1000

export interface JobStreamHandlers {
  /** 收到一帧事件。终态事件也会从这里过一遍，然后连接自动关闭。 */
  onEvent: (event: JobStreamEvent) => void
  /** 降级为轮询时回调一次（用于在界面上说明「为什么不再实时了」）。 */
  onFallback?: () => void
  /** 订阅彻底失败。 */
  onError?: (message: string) => void
}

/**
 * 订阅作业事件流：SSE 优先，连续 2 次订阅失败后降级为 1s 轮询。
 *
 * 用原生 `EventSource` 而不是 fetch + ReadableStream：`Last-Event-ID` 续传、
 * 断线自动重连都是它的内建行为，自己实现等于把它们重写一遍还没有它稳。
 *
 * 返回关闭函数 —— 作业结束、组件卸载、切换项目时都必须调，否则连接会一直挂着。
 */
export function streamJob(
  jobId: number,
  handlers: JobStreamHandlers,
  afterSeq = 0,
): () => void {
  let source: EventSource | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let cursor = afterSeq
  let failures = 0
  let closed = false

  const cleanup = () => {
    closed = true
    source?.close()
    source = null
    if (pollTimer !== null) clearInterval(pollTimer)
    pollTimer = null
  }

  const deliver = (event: JobStreamEvent) => {
    if (closed) return
    cursor = Math.max(cursor, event.seq)
    handlers.onEvent(event)
    if (STREAM_END_EVENT_TYPES.includes(event.type)) cleanup()
  }

  const startPolling = () => {
    if (closed || pollTimer !== null) return
    handlers.onFallback?.()
    const tick = async () => {
      if (closed) return
      try {
        const [job, events] = await Promise.all([
          api.getJob(jobId),
          api.getJobEvents(jobId, cursor),
        ])
        for (const event of events) deliver(event)
        // 订阅晚于终态：事件已被 after_seq 跳过，补一帧终态就收工
        if (!closed && events.length === 0 && TERMINAL_JOB_STATUSES.includes(job.status)) {
          deliver({
            seq: cursor,
            type: `job.${job.status}`,
            payload: { status: job.status, run_id: job.run_id, error: job.error },
          })
        }
      } catch (err) {
        cleanup()
        handlers.onError?.(err instanceof Error ? err.message : '订阅作业失败')
      }
    }
    pollTimer = setInterval(() => void tick(), POLL_INTERVAL_MS)
    void tick()
  }

  const open = () => {
    if (closed) return
    source = new EventSource(`/api/jobs/${jobId}/stream?after_seq=${cursor}`)
    source.onmessage = (message) => {
      failures = 0
      try {
        deliver(JSON.parse(message.data) as JobStreamEvent)
      } catch {
        // 单帧解析失败不该打断整条流
      }
    }
    source.onerror = () => {
      failures += 1
      if (failures < SSE_MAX_FAILURES) return // EventSource 会自己重连（带 Last-Event-ID）
      source?.close()
      source = null
      startPolling()
    }
  }

  open()
  return cleanup
}
