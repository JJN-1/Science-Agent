import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { App as AntdApp, Form, Input, Modal, Select, Tooltip } from 'antd'
import { api, ApiError } from '../api/client'
import type { Project, ToolInfo } from '../api/types'
import { money } from './UsagePanel'
import SettingsDialog from './SettingsDialog'

const PERMISSION_LABEL: Record<string, string> = {
  read: '只读',
  execute: '执行',
  dangerous: '危险',
}

export default function Sidebar() {
  const { message } = AntdApp.useApp()
  const { id } = useParams()
  const navigate = useNavigate()
  const [projects, setProjects] = useState<Project[]>([])
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [cost, setCost] = useState<{ total: number; calls: number } | null>(null)
  const [open, setOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [form] = Form.useForm()

  const load = useCallback(async () => {
    try {
      setProjects(await api.listProjects())
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '加载项目失败')
    }
  }, [message])

  useEffect(() => {
    void load()
  }, [load, id])

  // 工具清单与累计成本是全局资产，挂载一次即可；失败静默（侧栏不是报错的地方）
  useEffect(() => {
    let alive = true
    const loadSide = async () => {
      try {
        const list = await api.listTools()
        if (alive) setTools(list)
      } catch {
        // 注册表未装配（503）等：侧栏留空即可
      }
      try {
        const summary = await api.usageSummary('provider')
        if (alive) setCost({ total: summary.total_cost, calls: summary.total_calls })
      } catch {
        // 同上
      }
    }
    void loadSide()
    return () => {
      alive = false
    }
  }, [])

  const handleCreate = async () => {
    const values = await form.validateFields()
    try {
      const created = await api.createProject(values)
      message.success(`项目已创建：${created.title}`)
      setOpen(false)
      form.resetFields()
      navigate(`/projects/${created.id}`)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '创建失败')
    }
  }

  return (
    <nav className="sidebar">
      <div className="side-label">projects</div>
      {projects.map((p) => (
        <Link
          key={p.id}
          to={`/projects/${p.id}`}
          className={`side-item${String(p.id) === id ? ' active' : ''}`}
        >
          <span className={`dot s-${p.status}`} aria-hidden="true" />
          <span className="title">{p.title}</span>
        </Link>
      ))}
      {projects.length === 0 && (
        <div className="side-item" style={{ cursor: 'default', color: 'var(--faint)' }}>
          <span className="title">（还没有项目）</span>
        </div>
      )}

      {/* ── US-404：内核工具清单（数据源是运行中的注册表） ── */}
      {tools.length > 0 && (
        <>
          <div className="side-label">tools · {tools.length}</div>
          {tools.map((t) => (
            <Tooltip
              key={t.name}
              title={`${t.description}（可调用：${t.allowed_agents.join('、') || '无'}）`}
              placement="right"
            >
              <div className="side-item side-tool" style={{ cursor: 'default' }}>
                <span className={`perm p-${t.permission}`}>{PERMISSION_LABEL[t.permission] ?? t.permission}</span>
                <span className="title">{t.name}</span>
              </div>
            </Tooltip>
          ))}
        </>
      )}
      {cost !== null && (
        <div className="side-cost">
          累计 {money(cost.total)} · {cost.calls} 次调用
        </div>
      )}

      <div className="side-foot">
        <button
          className="btn"
          style={{ width: '100%', marginBottom: 8 }}
          onClick={() => setSettingsOpen(true)}
        >
          ⚙ 设置 · 模型后端
        </button>
        <button className="btn primary" style={{ width: '100%' }} onClick={() => setOpen(true)}>
          ＋ 新建项目
        </button>
      </div>

      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />

      <Modal
        title="新建研究项目"
        open={open}
        onOk={handleCreate}
        onCancel={() => setOpen(false)}
        okText="创建"
        cancelText="取消"
      >
        <Form form={form} layout="vertical" initialValues={{ domain: 'cs-ai' }}>
          <Form.Item
            name="title"
            label="项目标题"
            rules={[{ required: true, message: '请输入标题' }]}
          >
            <Input placeholder="例如：图神经网络在推荐系统中的应用" />
          </Form.Item>
          <Form.Item name="domain" label="领域">
            <Select options={[{ value: 'cs-ai', label: '计算机 / 人工智能' }]} />
          </Form.Item>
          <Form.Item name="goal" label="研究目标">
            <Input.TextArea rows={3} placeholder="一句话描述研究方向" />
          </Form.Item>
        </Form>
      </Modal>
    </nav>
  )
}
