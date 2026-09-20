import { useState } from 'react'
import { App as AntdApp, Button, Checkbox, Form, Input, InputNumber, Select, Tag } from 'antd'
import { api, ApiError } from '../api/client'
import type { HealthState, ProviderHealth, ProviderInput, ProviderTypes } from '../api/types'

export function healthLabel(state: HealthState): string {
  if (state === 'ok') return '可用'
  if (state === 'unconfigured') return '未配置密钥'
  return '不可达'
}

interface FormValues {
  /** 身份，仅新建时可填；留空按名称派生。 */
  id?: string
  /** 展示名：可改、可中文。 */
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

function stringifyJsonMap(raw: Record<string, unknown> | undefined): string | undefined {
  return raw && Object.keys(raw).length > 0 ? JSON.stringify(raw) : undefined
}

/**
 * 接入 / 编辑一个模型后端（US-312）。提交即生效，无需重启。
 *
 * 三件事在这里收口（旧实现把它们拆散在三处，于是互相打架）：
 * 1. **身份与名称分开**：`id` 是路由/凭据认的那个键，`name` 只是显示用，改名不重建；
 * 2. **Key 就在表单里**：不再有独立的「API Key」区块，填完一次保存即完整落位；
 * 3. **回填完整**：base_url / 凭据引用 / 免鉴权 / 超时 / 透传字段全部按现值回填，
 *    避免「打开编辑页看到一片空白，随手保存就把配置改坏了」。
 */
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
      name: values.name,
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
      if (values.keyless) {
        payload.api_key_ref = '' // 显式空串 = 该端点无需鉴权
      } else {
        // 留空 → 用该 provider 的 id 作为凭据引用名（后端缺省语义）；
        // 新建时省略该字段，交给后端按派生出的 id 落位
        const ref = (values.api_key_ref ?? '').trim() || initial?.id || ''
        payload.api_key_ref = ref || undefined
      }
    }

    setSaving(true)
    try {
      let providerId = initial?.id ?? ''
      if (isEdit) {
        await api.updateProvider(initial.id, payload)
      } else {
        const created = await api.createProvider({
          ...payload,
          ...(values.id?.trim() ? { id: values.id.trim() } : {}),
        })
        providerId = created.provider
      }
      // Key 与配置在同一次提交里落位：不会出现「配置存了、Key 没存」的半残状态
      if (values.api_key && providerId && !isMock && !values.keyless) {
        await api.setProviderKey(providerId, values.api_key)
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
    const providerId = initial?.id || values.id?.trim() || 'draft'
    setProbing(true)
    try {
      const result = await api.probeModels(providerId, {
        base_url: values.base_url,
        api_key: values.api_key,
        // 草稿的凭据语义必须与保存路径一致，否则会出现「保存后能用、探测说缺 Key」
        keyless: values.keyless === true,
        api_key_ref: values.keyless ? undefined : values.api_key_ref?.trim() || undefined,
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
        id: initial.id,
        name: initial.name,
        type: initial.type === 'MockProvider' ? 'mock' : 'openai_compat',
        vendor: initial.vendor,
        base_url: initial.base_url ?? undefined,
        models: initial.models,
        capabilities: initial.capabilities,
        priceInput: initial.price?.input ?? 0,
        priceOutput: initial.price?.output ?? 0,
        timeout_s: initial.timeout_s ?? 120,
        // 免鉴权端点在编辑页必须原样回填，否则保存时会被悄悄改成「需要 Key」
        keyless: initial.auth_required === false,
        api_key_ref: initial.api_key_ref ?? undefined,
        extraHeaders: stringifyJsonMap(initial.extra_headers),
        extraBody: stringifyJsonMap(initial.extra_body),
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

  const credentialHint = initial?.auth_required
    ? initial.has_key
      ? `已录入（凭据引用名 ${initial.api_key_ref}）`
      : `未录入（凭据引用名 ${initial.api_key_ref}）`
    : undefined

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
          extra={isEdit ? `标识（不可改）：${initial.id}` : '展示用，可中文；标识留空按名称自动生成'}
          style={{ flex: 1 }}
        >
          <Input placeholder="如 智谱 AI" />
        </Form.Item>
        {!isEdit && (
          <Form.Item
            name="id"
            label="标识"
            extra="路由与凭据引用它，创建后不可改"
            style={{ flex: 1 }}
          >
            <Input placeholder="留空自动生成，如 zhipu" />
          </Form.Item>
        )}
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
          rules={isEdit ? [] : [{ required: true, message: '必填' }]}
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
                extra="留空则用该后端的标识；多个端点可共用一份凭据"
                style={{ flex: 1 }}
              >
                <Input placeholder={initial?.id ?? 'acme'} />
              </Form.Item>
              <Form.Item
                name="api_key"
                label="API Key"
                extra={credentialHint ?? '写入 Windows 凭据管理器，不落配置文件'}
                style={{ flex: 1 }}
              >
                <Input.Password
                  placeholder={initial?.has_key ? '留空则不改动' : '粘贴 Key'}
                />
              </Form.Item>
              {initial && (
                <Form.Item label=" " style={{ width: 90 }}>
                  <Tag color={initial.has_key ? 'green' : 'orange'}>
                    {initial.has_key ? '已录入' : '未录入'}
                  </Tag>
                </Form.Item>
              )}
            </div>
          )}
        </>
      )}

      <details className="form-advanced">
        <summary>高级：透传字段（JSON）</summary>
        <Form.Item name="extraHeaders" label="extra_headers" style={{ marginTop: 8 }}>
          <Input.TextArea rows={2} placeholder='{"x-api-key": "abc"}' />
        </Form.Item>
        <Form.Item name="extraBody" label="extra_body">
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
