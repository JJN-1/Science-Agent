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
