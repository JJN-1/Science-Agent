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

export interface ProviderHealth {
  name: string
  type: string
  model: string
  vendor: string
  capabilities: string[]
  healthy: boolean
  circuit_failures: number
}

export interface TierRoute {
  provider: string
  model: string
}
