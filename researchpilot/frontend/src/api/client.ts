import type { AgentRun, BlackboardItem, Project, RunDetail, StageInfo } from './types'

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
  listStages: () => request<StageInfo[]>('/stages'),
  runStage: (projectId: number, stageId: string) =>
    request<{ run_id: number; status: string }>(
      `/projects/${projectId}/stages/${stageId}/run`,
      { method: 'POST' },
    ),
  listRuns: (projectId: number) => request<AgentRun[]>(`/projects/${projectId}/runs`),
  getRun: (runId: number) => request<RunDetail>(`/runs/${runId}`),
  getBlackboard: (projectId: number) =>
    request<BlackboardItem[]>(`/projects/${projectId}/blackboard`),
}
