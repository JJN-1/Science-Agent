import { useState } from 'react'
import { App as AntdApp, Button, Checkbox, Form, Input, InputNumber, Select } from 'antd'
import { api, ApiError } from '../api/client'
import type { HealthState, ProviderHealth, ProviderInput, ProviderTypes } from '../api/types'

export function healthLabel(state: HealthState): string {
  if (state === 'ok') return '可用'
  if (state === 'unconfigured') return '未配置密钥'
  return '不可达'
}

interface FormValues {
  name: string
  type: string
  base_url?: string
  vendor: string
  models: string[]
  capabilities: string[]
  priceInput: number
  priceOutput: number
  timeout_s: number
  keyless: boolean
  api_key_ref?: string
  api_key?: string
  extraHeaders?: string
  extraBody?: string
}

function parseJsonMap(raw: string | undefined, label: string): Record<string, unknown> | null {
  if (!raw?.trim()) return null
  const parsed = JSON.parse(raw)
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${label} 必须是一个 JSON 对象`)
  }
  return parsed as Record<string, unknown>
}

/** 接入 / 编辑一个模型后端（US-312）。提交即生效，无需重启。 */
export default function ProviderForm({
  initial,
  types,
  onSaved,
  onCancel,
}: {
  initial: ProviderHealth | null
  types: ProviderTypes
  onSaved: () => void
  onCancel: () => void
}) {
  const { message } = AntdApp.useApp()
  const [form] = Form.useForm<FormValues>()
  const [saving, setSaving] = useState(false)
  const [probing, setProbing] = useState(false)

  const isEdit = initial !== null
  const isMock = Form.useWatch('type', form) === 'mock'
  const keyless = Form.useWatch('keyless', form) === true

  const submit = async () => {
    let values: FormValues
    try {
      values = await form.validateFields()
    } catch {
      return
    }

    let extraHeaders: Record<string, unknown> | null
    let extraBody: Record<string, unknown> | null
    try {
      extraHeaders = parseJsonMap(values.extraHeaders, 'extra_headers')
      extraBody = parseJsonMap(values.extraBody, 'extra_body')
    } catch (err) {
      message.error(err instanceof Error ? err.message : 'JSON 字段格式不正确')
      return
    }

    const payload: ProviderInput = {
      type: values.type,
      vendor: values.vendor,
      models: values.models,
      capabilities: values.capabilities,
      price: { input: values.priceInput ?? 0, output: values.priceOutput ?? 0 },
      timeout_s: values.timeout_s,
      extra_headers: (extraHeaders as Record<string, string> | null) ?? null,
      extra_body: extraBody,
    }
    if (!isMock) {
      payload.base_url = values.base_url
      // 显式空串 = 该端点无需鉴权
      payload.api_key_ref = values.keyless ? '' : (values.api_key_ref || values.name)
    }

    setSaving(true)
    try {
      if (isEdit) {
        await api.updateProvider(values.name, payload)
      } else {
        await api.createProvider({ name: values.name, ...payload })
      }
      if (values.api_key && !isMock) {
        await api.setProviderKey(values.name, values.api_key)
      }
      message.success(`${values.name} 已保存并生效`)
      onSaved()
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const probe = async (fill: boolean) => {
    const values = form.getFieldsValue()
    if (!values.name) {
      message.warning('请先填写 provider 名称')
      return
    }
    setProbing(true)
    try {
      const result = await api.probeModels(values.name, {
        base_url: values.base_url,
        api_key: values.api_key,
      })
      if (!result.ok) {
        message.warning(result.detail ?? '端点未返回模型清单')
        return
      }
      if (fill) {
        form.setFieldValue('models', result.models)
        message.success(`探测到 ${result.models.length} 个模型，已填入模型清单`)
      } else {
        message.success(`连通性正常，端点报告 ${result.models.length} 个模型`)
      }
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '探测失败')
    } finally {
      setProbing(false)
    }
  }

  const defaults: Partial<FormValues> = initial
    ? {
        name: initial.name,
        type: initial.type === 'MockProvider' ? 'mock' : 'openai_compat',
        vendor: initial.vendor,
        models: initial.models,
        capabilities: initial.capabilities,
        priceInput: initial.price?.input ?? 0,
        priceOutput: initial.price?.output ?? 0,
      }
    : {
        type: 'openai_compat',
        capabilities: ['json_object'],
        models: [],
        priceInput: 0,
        priceOutput: 0,
        timeout_s: 120,
        keyless: false,
      }

  return (
    <Form<FormValues>
      form={form}
      layout="vertical"
      size="small"
      initialValues={{ timeout_s: 120, keyless: false, ...defaults }}
      onFinish={submit}
    >
      <div className="form-row">
        <Form.Item
          name="name"
          label="名称"
          rules={[{ required: true, message: '必填' }]}
          style={{ flex: 1 }}
        >
          <Input placeholder="唯一标识，如 acme" disabled={isEdit} />
        </Form.Item>
        <Form.Item name="type" label="类型" rules={[{ required: true }]} style={{ width: 170 }}>
          <Select
            options={types.types.map((t) => ({ value: t, label: t }))}
            disabled={isEdit}
          />
        </Form.Item>
        <Form.Item name="vendor" label="厂商" rules={[{ required: true, message: '必填' }]} style={{ width: 150 }}>
          <Input placeholder="critique 跨厂商校验用" />
        </Form.Item>
      </div>

      {!isMock && (
        <Form.Item
          name="base_url"
          label="Base URL"
          rules={[{ required: true, message: '必填' }]}
          extra="OpenAI 兼容端点根地址，如 https://api.example.com/v1；本机自建可填 http://127.0.0.1:11434/v1"
        >
          <Input placeholder="https://api.example.com/v1" />
        </Form.Item>
      )}

      <Form.Item label="模型清单" required>
        <div className="form-inline">
          <Form.Item name="models" noStyle rules={[{ required: true, message: '至少填一个模型 ID' }]}>
            <Select
              mode="tags"
              placeholder="回车输入模型 ID；也可点右侧按钮自动探测"
              tokenSeparators={[',', ' ']}
              options={(initial?.models ?? []).map((m) => ({ value: m, label: m }))}
            />
          </Form.Item>
          {!isMock && (
            <Button loading={probing} onClick={() => void probe(true)}>
              探测模型
            </Button>
          )}
        </div>
      </Form.Item>

      <Form.Item name="capabilities" label="能力声明" extra="只取本地声明，不从上游清单推断">
        <Checkbox.Group
          options={types.capabilities.map((c) => ({ value: c, label: c }))}
        />
      </Form.Item>

      <div className="form-row">
        <Form.Item name="priceInput" label="输入单价（元 / 1K token）" style={{ flex: 1 }}>
          <InputNumber min={0} step={0.001} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="priceOutput" label="输出单价（元 / 1K token）" style={{ flex: 1 }}>
          <InputNumber min={0} step={0.001} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="timeout_s" label="超时（秒）" style={{ width: 130 }}>
          <InputNumber min={1} style={{ width: '100%' }} />
        </Form.Item>
      </div>

      {!isMock && (
        <>
          <Form.Item name="keyless" valuePropName="checked">
            <Checkbox>该端点无需鉴权（本地自建端点）</Checkbox>
          </Form.Item>
          {!keyless && (
            <div className="form-row">
              <Form.Item
                name="api_key_ref"
                label="凭据引用名"
                extra="留空则用 provider 名称"
                style={{ flex: 1 }}
              >
                <Input placeholder={initial?.name ?? 'acme'} />
              </Form.Item>
              <Form.Item
                name="api_key"
                label="API Key"
                extra="写入 Windows 凭据管理器，不落配置文件"
                style={{ flex: 1 }}
              >
                <Input.Password placeholder={isEdit ? '留空则不改动' : '粘贴 Key'} />
              </Form.Item>
            </div>
          )}
        </>
      )}

      <details className="form-advanced">
        <summary>高级：透传字段（JSON）</summary>
        <Form.Item name="extraHeaders" label="extra_headers" style={{ marginTop: 8 }}>
          <Input.TextArea rows={2} placeholder='{"x-api-key": "abc"}' />
        </Form.Item>
        <Form.Item name="extra_body" label="extra_body">
          <Input.TextArea rows={2} placeholder='{"top_p": 0.9}' />
        </Form.Item>
      </details>

      <div className="form-actions">
        {!isMock && (
          <Button loading={probing} onClick={() => void probe(false)}>
            连通性测试
          </Button>
        )}
        <span className="spacer" />
        <Button onClick={onCancel}>取消</Button>
        <Button type="primary" htmlType="submit" loading={saving}>
          保存并生效
        </Button>
      </div>
    </Form>
  )
}
