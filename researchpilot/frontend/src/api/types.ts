export interface Project {
  id: number
  title: string
  domain: string
  goal: string
  status: string
  created_at: string
  updated_at: string
}

export interface StageInfo {
  stage_id: string
  agent_id: string
  name: string
  description: string
  implemented: boolean
  planned_sprint: number | null
}

export interface AgentStep {
  id: number
  run_id: number
  seq: number
  kind: string
  content: Record<string, unknown>
  created_at: string
}

export interface AgentRun {
  id: number
  project_id: number
  stage_id: string
  agent_id: string
  status: string
  steps: number
  error: string | null
  started_at: string
  finished_at: string | null
}

export interface RunDetail extends Omit<AgentRun, 'steps'> {
  steps: AgentStep[]
}

export interface BlackboardItem {
  id: number
  obj_type: string
  version: number
  payload: Record<string, unknown>
  produced_by: string
  evidence: unknown[]
  created_at: string
}

export interface Decision {
  id: number
  run_id: number | null
  stage_id: string
  agent_id: string
  kind: 'decision' | 'failed_attempt' | string
  decision: string
  reason: string
  alternatives: unknown[]
  decided_by: string
  created_at: string
}

export interface UsageRow {
  key: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  cost: number
}

export interface UsageSummary {
  dim: string
  project_id: number
  rows: UsageRow[]
  total_cost: number
}

export interface Approval {
  id: number
  project_id: number
  run_id: number | null
  kind: string
  detail: Record<string, unknown>
  status: string
  created_at: string
  decided_at: string | null
}

export type HealthState = 'ok' | 'unconfigured' | 'down' | string

/** provider 来源：内置（default.yaml）/ 用户接入（config.yaml）/ 仅运行期 */
export type ProviderSource = 'builtin' | 'user' | 'runtime'

export interface ProviderHealth {
  name: string
  type: string
  model: string
  models: string[]
  vendor: string
  capabilities: string[]
  price: { input: number; output: number }
  health: HealthState
  healthy: boolean
  circuit_failures: number
  detail: string | null
  /** 引用位置，如 routing:plan / agent:scout */
  referenced_by: string[]
  source: ProviderSource
  deletable: boolean
}

/** 接入表单提交体（US-312）；未提交的字段在 PATCH 时沿用现值 */
export interface ProviderInput {
  name?: string
  type?: string
  base_url?: string | null
  models?: string[]
  vendor?: string
  api_key_ref?: string | null
  capabilities?: string[]
  price?: { input: number; output: number }
  timeout_s?: number
  extra_headers?: Record<string, string> | null
  extra_body?: Record<string, unknown> | null
}

export interface ProviderTypes {
  types: string[]
  capabilities: string[]
}

export interface ProbeResult {
  provider: string
  ok: boolean
  models: string[]
  detail: string | null
}

export interface TierRoute {
  provider: string
  model: string
}

// ── US-304：异步作业与事件流 ──────────────────────

/** 作业状态机：queued → running → succeeded / failed / paused */
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'paused'

export interface Job {
  id: number
  project_id: number
  kind: 'stage' | 'pipeline' | string
  stage_id: string | null
  status: JobStatus | string
  run_id: number | null
  error: string | null
  params: Record<string, unknown>
  created_at: string
  started_at: string | null
  finished_at: string | null
}

/** SSE 帧里的形状（id 走 SSE 的 id 行，不重复进 data）。 */
export interface JobStreamEvent {
  seq: number
  type: string
  payload: Record<string, unknown>
}

/** 轮询接口多带出来的两个字段。 */
export interface JobEvent extends JobStreamEvent {
  id: number
  created_at: string
}
