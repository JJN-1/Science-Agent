import type {
  AgentRun,
  Approval,
  BlackboardItem,
  Decision,
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
  runStage: (projectId: number, stageId: string) =>
    request<{ run_id: number; status: string }>(
      `/projects/${projectId}/stages/${stageId}/run`,
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
