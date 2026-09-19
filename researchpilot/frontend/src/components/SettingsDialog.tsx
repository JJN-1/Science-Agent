import { useCallback, useEffect, useState } from 'react'
import { App as AntdApp, Input, Modal, Popconfirm, Table } from 'antd'
import { api, ApiError } from '../api/client'
import type { ProviderHealth, ProviderTypes, TierRoute } from '../api/types'
import ProviderForm, { healthLabel } from './ProviderForm'

const TIERS = ['extract', 'plan', 'critique', 'synthesize', 'write'] as const

const EMPTY_TYPES: ProviderTypes = { types: [], capabilities: [] }

/** 设置对话框（US-206 / US-312）：模型后端接入、档位路由热切换、Key 录入、热重载 */
export default function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { message } = AntdApp.useApp()
  const [providers, setProviders] = useState<ProviderHealth[]>([])
  const [routing, setRouting] = useState<Record<string, TierRoute[]>>({})
  const [draft, setDraft] = useState<Record<string, TierRoute>>({})
  const [keys, setKeys] = useState<Record<string, string>>({})
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

  const saveTier = async (tier: string) => {
    const d = draft[tier]
    if (!d?.provider || !d.model) return
    try {
      await api.patchRouting(tier, [d])
      message.success(`档位 ${tier} 已热切换`)
      await load()
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '切换失败')
    }
  }

  const saveKey = async (name: string) => {
    const key = keys[name]
    if (!key) return
    try {
      await api.setProviderKey(name, key)
      message.success(`${name} 的 API Key 已写入凭据管理器`)
      setKeys((k) => ({ ...k, [name]: '' }))
      await load()
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '写入失败')
    }
  }

  const remove = async (name: string) => {
    try {
      await api.deleteProvider(name)
      message.success(`${name} 已移除`)
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
    <Modal title="设置 · 模型后端与档位路由" open={open} onCancel={onClose} footer={null} width={860}>
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
          rowKey="name"
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
            { title: '名称', dataIndex: 'name', width: 90 },
            { title: '类型', dataIndex: 'type', width: 120 },
            {
              title: '模型',
              dataIndex: 'models',
              render: (models: string[]) => models.join(', '),
            },
            { title: '厂商', dataIndex: 'vendor', width: 80 },
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
                    onConfirm={() => remove(row.name)}
                  >
                    <button className="btn tiny" disabled={!row.deletable}>删除</button>
                  </Popconfirm>
                </span>
              ),
            },
          ]}
        />
        <div className="settings-hint">
          接入表单写入用户 config.yaml，保存即刻生效、无需重启。Key 只进 Windows 凭据管理器。
        </div>
      </div>

      <div className="settings-section">
        <div className="settings-head">
          <span>API Key（写入凭据管理器）</span>
        </div>
        {providers.filter((p) => p.type !== 'MockProvider').length === 0 && (
          <div className="settings-hint">当前没有需要密钥的后端。</div>
        )}
        {providers
          .filter((p) => p.type !== 'MockProvider')
          .map((row) => (
            <div className="tier-row" key={row.name}>
              <span className="tier-name">{row.name}</span>
              <Input.Search
                size="small"
                type="password"
                style={{ width: 320 }}
                placeholder="粘贴 API Key"
                value={keys[row.name] ?? ''}
                onChange={(e) => setKeys((k) => ({ ...k, [row.name]: e.target.value }))}
                onSearch={() => saveKey(row.name)}
                enterButton="保存"
              />
              {row.detail && <span className="tier-current">{row.detail}</span>}
            </div>
          ))}
      </div>

      <div className="settings-section">
        <div className="settings-head">
          <span>档位路由（修改即热切换，无需重启）</span>
        </div>
        {TIERS.map((tier) => (
          <div className="tier-row" key={tier}>
            <span className="tier-name">{tier}</span>
            <Input
              size="small"
              style={{ width: 130 }}
              value={draft[tier]?.provider ?? ''}
              placeholder="provider"
              onChange={(e) =>
                setDraft((d) => ({ ...d, [tier]: { ...d[tier], provider: e.target.value } }))
              }
            />
            <Input
              size="small"
              style={{ width: 180 }}
              value={draft[tier]?.model ?? ''}
              placeholder="model"
              onChange={(e) =>
                setDraft((d) => ({ ...d, [tier]: { ...d[tier], model: e.target.value } }))
              }
            />
            <button className="btn tiny" onClick={() => saveTier(tier)}>
              应用
            </button>
            {routing[tier]?.[0] && (
              <span className="tier-current">
                当前：{routing[tier][0].provider}/{routing[tier][0].model}
              </span>
            )}
          </div>
        ))}
      </div>

      <Modal
        title={editing === 'new' ? '接入新模型后端' : `编辑 ${editing?.name ?? ''}`}
        open={editing !== null}
        onCancel={closeForm}
        footer={null}
        width={720}
        destroyOnHidden
      >
        {editing !== null && (
          <ProviderForm
            key={editing === 'new' ? '__new__' : editing.name}
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
