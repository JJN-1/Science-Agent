import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { App as AntdApp, Form, Input, Modal, Select } from 'antd'
import { api, ApiError } from '../api/client'
import type { Project } from '../api/types'

export default function Sidebar() {
  const { message } = AntdApp.useApp()
  const { id } = useParams()
  const navigate = useNavigate()
  const [projects, setProjects] = useState<Project[]>([])
  const [open, setOpen] = useState(false)
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
      <div className="side-foot">
        <button className="btn primary" style={{ width: '100%' }} onClick={() => setOpen(true)}>
          ＋ 新建项目
        </button>
      </div>

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
