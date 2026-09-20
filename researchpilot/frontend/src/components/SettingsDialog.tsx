import { useCallback, useEffect, useState } from 'react'
import { App as AntdApp, AutoComplete, Modal, Popconfirm, Select, Table, Tag } from 'antd'
import { api, ApiError } from '../api/client'
import type { ProviderHealth, ProviderTypes, TierRoute } from '../api/types'
import ProviderForm, { healthLabel } from './ProviderForm'

const TIERS = ['extract', 'plan', 'critique', 'synthesize', 'write'] as const

const EMPTY_TYPES: ProviderTypes = { types: [], capabilities: [] }

/** 凭据一览（表内一列）：一眼看出哪个后端还没配好。 */
function CredentialTag({ row }: { row: ProviderHealth }) {
  if (row.type === 'MockProvider') return <span style={{ color: 'var(--faint)' }}>—</span>
  if (!row.auth_required) return <Tag>无需鉴权</Tag>
  return <Tag color={row.has_key ? 'green' : 'orange'}>{row.has_key ? '已录入' : '未录入'}</Tag>
}

/**
 * 设置对话框（US-206 / US-312）：模型后端接入、档位路由热切换、热重载。
 *
 * Key 已并入接入表单：不再有独立的「API Key」区块 —— 一个后端该配什么，
 * 就在同一张表里配完。这里只负责列表、路由与重载。
 */
export default function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { message } = AntdApp.useApp()
  const [providers, setProviders] = useState<ProviderHealth[]>([])
  const [routing, setRouting] = useState<Record<string, TierRoute[]>>({})
  const [draft, setDraft] = useState<Record<string, TierRoute>>({})
  const [types, setTypes] = useState<ProviderTypes>(EMPTY_TYPES)
  const [editing, setEditing] = useState<ProviderHealth | 'new' | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [p, r, t] = await Promise.all([
        api.getProviders(),
        api.getRouting(),
        api.getProviderTypes(),
      ])
      setProviders(p)
      setRouting(r)
      setTypes(t)
      setDraft(
        Object.fromEntries(
          TIERS.map((tier) => [tier, r[tier]?.[0] ?? { provider: '', model: '' }]),
        ) as Record<string, TierRoute>,
      )
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载设置失败')
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  const modelsOf = (providerId: string) =>
    providers.find((p) => p.id === providerId)?.models ?? []

  const providerOptions = (current: string) => {
    const opts = providers.map((p) => ({ value: p.id, label: `${p.name}（${p.id}）` }))
    // 引用的 provider 已被移除时，别让下拉框变成一片空白
    return opts.some((o) => o.value === current) || !current
      ? opts
      : [{ value: current, label: `${current}（已移除）` }, ...opts]
  }

  const saveTier = async (tier: string) => {
    const d = draft[tier]
    const provider = d?.provider?.trim()
    const model = d?.model?.trim()
    if (!provider || !model) {
      message.warning('请先选择后端并填写模型 ID')
      return
    }
    // 只改首候选，**保留**配置好的降级链尾 —— 旧实现把整条链替换成单个候选，
    // 用户点一下「应用」就把 fallback 全清空了
    const tail = (routing[tier] ?? []).slice(1)
    try {
      await api.patchRouting(tier, [{ provider, model }, ...tail])
      message.success(`档位 ${tier} 已热切换`)
      await load()
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '切换失败')
    }
  }

  const remove = async (id: string) => {
    try {
      await api.deleteProvider(id)
      message.success(`${id} 已移除`)
      await load()
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '删除失败')
    }
  }

  const reload = async () => {
    try {
      const delta = await api.reloadProviders()
      message.success(`热重载完成：${delta.providers.join(', ') || '（空）'}`)
      await load()
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '热重载失败')
    }
  }

  const closeForm = () => setEditing(null)

  return (
    <Modal title="设置 · 模型后端与档位路由" open={open} onCancel={onClose} footer={null} width={960}>
      <div className="settings-section">
        <div className="settings-head">
          <span>模型后端</span>
          <span className="spacer" />
          <button className="btn tiny" onClick={() => setEditing('new')}>＋ 接入新后端</button>
          <button className="btn tiny" onClick={reload}>⟳ 热重载</button>
        </div>
        <Table<ProviderHealth>
          size="small"
          loading={loading}
          rowKey="id"
          dataSource={providers}
          pagination={false}
          columns={[
            {
              title: '',
              dataIndex: 'healthy',
              width: 24,
              render: (healthy: boolean, row: ProviderHealth) => (
                <span
                  className={`dot s-${healthy ? 'succeeded' : 'failed'}`}
                  style={{ display: 'inline-block' }}
                  title={row.detail ?? healthLabel(row.health)}
                />
              ),
            },
            { title: '名称', dataIndex: 'name', width: 130 },
            {
              title: '标识',
              dataIndex: 'id',
              width: 120,
              render: (id: string) => <span className="provider-id">{id}</span>,
            },
            {
              title: '模型',
              dataIndex: 'models',
              render: (models: string[]) => models.join(', '),
            },
            { title: '厂商', dataIndex: 'vendor', width: 80 },
            {
              title: '凭据',
              dataIndex: 'has_key',
              width: 90,
              render: (_: unknown, row: ProviderHealth) => <CredentialTag row={row} />,
            },
            {
              title: '状态',
              dataIndex: 'health',
              width: 100,
              render: (health: string, row: ProviderHealth) =>
                health === 'ok' && row.healthy ? '可用' : healthLabel(health),
            },
            {
              title: '被引用',
              dataIndex: 'referenced_by',
              width: 120,
              render: (refs: string[]) =>
                refs.length ? refs.join('、') : <span style={{ color: 'var(--faint)' }}>无</span>,
            },
            {
              title: '操作',
              width: 110,
              render: (_: unknown, row: ProviderHealth) => (
                <span className="row-actions">
                  <button
                    className="btn tiny"
                    disabled={row.source === 'builtin'}
                    onClick={() => setEditing(row)}
                  >
                    编辑
                  </button>
                  <Popconfirm
                    title={`移除 ${row.name}？`}
                    description="被档位路由或 Agent 引用时会被拒绝。"
                    okText="移除"
                    cancelText="取消"
                    disabled={!row.deletable}
                    onConfirm={() => remove(row.id)}
                  >
                    <button className="btn tiny" disabled={!row.deletable}>删除</button>
                  </Popconfirm>
                </span>
              ),
            },
          ]}
        />
        <div className="settings-hint">
          接入表单写入用户 config.yaml，保存即刻生效、无需重启；API Key 在同一张表单里填写，
          只进 Windows 凭据管理器。名称可随时修改，路由与凭据认的是「标识」。
        </div>
      </div>

      <div className="settings-section">
        <div className="settings-head">
          <span>档位路由（修改即热切换，无需重启）</span>
        </div>
        {TIERS.map((tier) => {
          const chain = routing[tier] ?? []
          const current = chain[0]
          return (
            <div className="tier-row" key={tier}>
              <span className="tier-name">{tier}</span>
              <Select
                size="small"
                style={{ width: 220 }}
                value={draft[tier]?.provider || undefined}
                placeholder="选择后端"
                options={providerOptions(draft[tier]?.provider ?? '')}
                onChange={(provider: string) => {
                  const first = modelsOf(provider)[0] ?? ''
                  setDraft((d) => ({ ...d, [tier]: { provider, model: first } }))
                }}
              />
              <AutoComplete
                size="small"
                style={{ width: 220 }}
                value={draft[tier]?.model ?? ''}
                placeholder="模型 ID"
                options={modelsOf(draft[tier]?.provider ?? '').map((m) => ({ value: m }))}
                onChange={(model: string) =>
                  setDraft((d) => ({ ...d, [tier]: { ...d[tier], model } }))
                }
              />
              <button className="btn tiny" onClick={() => saveTier(tier)}>
                应用
              </button>
              {current && (
                <span className="tier-current">
                  当前：{current.provider}/{current.model}
                  {chain.length > 1 && ` ＋ 备选 ${chain.length - 1} 个`}
                </span>
              )}
            </div>
          )
        })}
        <div className="settings-hint">
          首个候选不可用时自动走降级链；修改只作用于首候选，已有备选会保留。
          critique 档需要与 plan / synthesize 不同后端或不同模型，否则交叉验证失效。
          模型 ID 必须在该后端的模型清单里 —— 填错会在这一步被挡下，而不是等到真跑起来才发现。
        </div>
      </div>

      <Modal
        title={editing === 'new' ? '接入新模型后端' : `编辑 ${editing?.name ?? ''}`}
        open={editing !== null}
        onCancel={closeForm}
        footer={null}
        width={760}
        destroyOnHidden
      >
        {editing !== null && (
          <ProviderForm
            key={editing === 'new' ? '__new__' : editing.id}
            initial={editing === 'new' ? null : editing}
            types={types}
            onSaved={() => {
              closeForm()
              void load()
            }}
            onCancel={closeForm}
          />
        )}
      </Modal>
    </Modal>
  )
}
