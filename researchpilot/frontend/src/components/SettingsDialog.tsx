import { useCallback, useEffect, useState } from 'react'
import { App as AntdApp, Input, Modal, Table } from 'antd'
import { api, ApiError } from '../api/client'
import type { ProviderHealth, TierRoute } from '../api/types'

const TIERS = ['extract', 'plan', 'critique', 'synthesize', 'write'] as const

/** 设置对话框（US-206）：后端健康、档位路由热切换、Key 录入、热重载 */
export default function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { message } = AntdApp.useApp()
  const [providers, setProviders] = useState<ProviderHealth[]>([])
  const [routing, setRouting] = useState<Record<string, TierRoute[]>>({})
  const [draft, setDraft] = useState<Record<string, TierRoute>>({})
  const [keys, setKeys] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [p, r] = await Promise.all([api.getProviders(), api.getRouting()])
      setProviders(p)
      setRouting(r)
      setDraft(
        Object.fromEntries(
          TIERS.map((t) => [t, r[t]?.[0] ?? { provider: '', model: '' }]),
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

  const reload = async () => {
    try {
      const delta = await api.reloadProviders()
      message.success(`热重载完成：${delta.providers.join(', ') || '（空）'}`)
      await load()
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '热重载失败')
    }
  }

  return (
    <Modal title="设置 · 模型后端与档位路由" open={open} onCancel={onClose} footer={null} width={720}>
      <div className="settings-section">
        <div className="settings-head">
          <span>模型后端</span>
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
              render: (healthy: boolean) => (
                <span className={`dot s-${healthy ? 'succeeded' : 'failed'}`} style={{ display: 'inline-block' }} />
              ),
            },
            { title: '名称', dataIndex: 'name', width: 100 },
            { title: '类型', dataIndex: 'type', width: 130 },
            { title: '模型', dataIndex: 'model', width: 120 },
            { title: '厂商', dataIndex: 'vendor', width: 90 },
            {
              title: 'API Key（写入凭据管理器）',
              dataIndex: 'key',
              render: (_: unknown, row: ProviderHealth) =>
                row.type === 'MockProvider' ? (
                  <span style={{ color: 'var(--faint)' }}>无需 Key</span>
                ) : (
                  <Input.Search
                    size="small"
                    type="password"
                    placeholder="粘贴 API Key"
                    value={keys[row.name] ?? ''}
                    onChange={(e) => setKeys((k) => ({ ...k, [row.name]: e.target.value }))}
                    onSearch={() => saveKey(row.name)}
                    enterButton="保存"
                  />
                ),
            },
          ]}
        />
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
    </Modal>
  )
}
