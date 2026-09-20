import { useCallback, useEffect, useState } from 'react'
import { App as AntdApp, Segmented, Table, Tooltip } from 'antd'
import { api, ApiError } from '../api/client'
import type { UsageDim, UsageRow, UsageSummary } from '../api/types'

const DIMS: { value: UsageDim; label: string }[] = [
  { value: 'provider', label: '按后端' },
  { value: 'stage', label: '按阶段' },
  { value: 'agent', label: '按 Agent' },
]

/** 花费以元计（配置里的 price 就是元 / 1K token）；免费额度下会一直是 0。 */
export function money(value: number): string {
  return `¥${value.toFixed(value > 0 && value < 0.0001 ? 6 : 4)}`
}

function useUsage(dim: UsageDim, projectId: number | undefined, enabled: boolean) {
  const [data, setData] = useState<UsageSummary | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!enabled) return
    setLoading(true)
    try {
      setData(await api.usageSummary(dim, projectId))
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '加载用量失败')
    } finally {
      setLoading(false)
    }
  }, [dim, projectId, enabled])

  useEffect(() => {
    void load()
  }, [load])

  return { data, loading, error, reload: load }
}

/**
 * 用量统计（US-203）：把「每个后端被调过几次、失败几次、花了多少」摆到台面上。
 *
 * 失败次数是这张表的主角 —— 从前失败调用根本不落账，于是「我明明跑过一次，
 * 统计里却没有」成了必然；现在它既进 ``calls`` 也单列成 ``failed``。
 */
export function UsagePanel({ projectId }: { projectId?: number }) {
  const { message } = AntdApp.useApp()
  const [dim, setDim] = useState<UsageDim>('provider')
  const { data, loading, error, reload } = useUsage(dim, projectId, true)

  useEffect(() => {
    if (error) message.error(error)
  }, [error, message])

  const rows = data?.rows ?? []

  return (
    <div className="settings-section">
      <div className="settings-head usage-head">
        <span>用量统计</span>
        <Segmented
          size="small"
          value={dim}
          options={DIMS}
          onChange={(value) => setDim(value as UsageDim)}
        />
        <span className="spacer" />
        <span className="usage-scope">
          {projectId === undefined ? '全部项目' : `项目 #${projectId}`}
        </span>
        <button className="btn tiny" onClick={() => void reload()}>⟳ 刷新</button>
      </div>
      <Table<UsageRow>
        size="small"
        loading={loading}
        rowKey="key"
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: '还没有模型调用记录' }}
        columns={[
          {
            title: DIMS.find((d) => d.value === dim)?.label.replace('按', '') ?? '键',
            dataIndex: 'key',
            render: (key: string) => <span className="provider-id">{key || '—'}</span>,
          },
          {
            title: '调用',
            dataIndex: 'calls',
            width: 80,
            render: (calls: number, row: UsageRow) =>
              row.failed > 0 ? (
                <Tooltip title={`其中 ${row.failed} 次失败（失败调用不计花费）`}>
                  <span>{calls}</span>
                </Tooltip>
              ) : (
                calls
              ),
          },
          {
            title: '失败',
            dataIndex: 'failed',
            width: 70,
            render: (failed: number) =>
              failed > 0 ? <span className="usage-fail">{failed}</span> : <span className="usage-zero">0</span>,
          },
          { title: '输入 token', dataIndex: 'prompt_tokens', width: 110, align: 'right' as const },
          { title: '输出 token', dataIndex: 'completion_tokens', width: 110, align: 'right' as const },
          {
            title: '花费',
            dataIndex: 'cost',
            width: 110,
            align: 'right' as const,
            render: (cost: number) => money(cost),
          },
        ]}
        summary={() =>
          rows.length === 0
            ? null
            : (
              <Table.Summary.Row>
                <Table.Summary.Cell index={0}>合计</Table.Summary.Cell>
                <Table.Summary.Cell index={1}>{data?.total_calls ?? 0}</Table.Summary.Cell>
                <Table.Summary.Cell index={2}>
                  <span className={(data?.total_failed ?? 0) > 0 ? 'usage-fail' : 'usage-zero'}>
                    {data?.total_failed ?? 0}
                  </span>
                </Table.Summary.Cell>
                <Table.Summary.Cell index={3} />
                <Table.Summary.Cell index={4} />
                <Table.Summary.Cell index={5} align="right">
                  {money(data?.total_cost ?? 0)}
                </Table.Summary.Cell>
              </Table.Summary.Row>
            )
        }
      />
      <div className="settings-hint">
        每次模型调用都记账，失败也记（失败行花费为 0）；缓存命中的调用同样在列。
      </div>
    </div>
  )
}

/**
 * 会话页的一行用量摘要：跑完一轮就能立刻看到这次有没有真打到模型。
 *
 * ``version`` 由父组件在对账刷新后自增 —— 作业失败不一定会多出一条 run，
 * 靠 run 数量当刷新信号会漏掉「失败调用」这一类最该被看见的情况。
 */
export function UsageLine({ projectId, version }: { projectId: number; version: number }) {
  const [data, setData] = useState<UsageSummary | null>(null)

  const load = useCallback(async () => {
    try {
      setData(await api.usageSummary('provider', projectId))
    } catch {
      // 摘要失败不影响主流程，静默即可（统计面板里会给出明确报错）
    }
  }, [projectId])

  useEffect(() => {
    void load()
  }, [load, version])

  if (!data) return null
  if (data.total_calls === 0) {
    return <div className="hintline">{'// 本项目还没有模型调用记录'}</div>
  }

  return (
    <div className="usage-line">
      <span className="usage-key">用量</span>
      <span>调用 {data.total_calls} 次</span>
      {data.total_failed > 0 && (
        <span className="usage-fail">失败 {data.total_failed}</span>
      )}
      <span>花费 {money(data.total_cost)}</span>
      <span className="usage-detail">
        {data.rows.map((r) => `${r.key || '—'}×${r.calls}`).join(' · ')}
      </span>
    </div>
  )
}
