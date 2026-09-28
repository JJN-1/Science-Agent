import type { Message } from './types'

/** 工具调用卡的统一视图：落库消息对与流内事件都折叠成这个形状。 */
export interface ToolCallView {
  /** 稳定 key：assistant.tool_calls[].id 或流事件的 call_id */
  callId: string
  name: string
  /** 模型给的原始 arguments 字符串；解析失败的原文照样展示 */
  rawArgs: string
  /** 权限档位：read / execute / dangerous / unknown（流事件带，库里不带） */
  permission: string
  /** null = 只有 tool.call 没等到 tool.result（运行中或订阅断开） */
  ok: boolean | null
  durationMs: number | null
  error: string | null
  /** 结果摘要（tool.result 的 result_preview 或落库 tool 消息的正文） */
  result: string | null
  truncated: boolean
}

/** 解析模型给的原始 arguments 字符串；不是 JSON 对象就返回 null。 */
export function parseArgsSafe(raw: string): Record<string, unknown> | null {
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as unknown
    return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/**
 * 落库 tool 消息没有独立的状态列：失败是内核包的 ``{"ok": false, "error": ...}``
 * 信封，成功是工具自己的原始输出。这里按形状反推，不猜字符串前缀。
 */
function failureOf(content: string): string | null {
  try {
    const parsed = JSON.parse(content) as { ok?: unknown; error?: unknown }
    if (parsed && typeof parsed === 'object' && parsed.ok === false) {
      return typeof parsed.error === 'string' ? parsed.error : content
    }
  } catch {
    // 成功结果多数是普通文本，解析失败是常态而非异常
  }
  return null
}

/** 从落库消息对（assistant.tool_calls + role=tool 的结果）构建视图，D12 渲染用。 */
export function toolCallViewsOf(
  toolCalls: Message['tool_calls'],
  resultByCallId: Map<string, Message>,
  permByTool: Record<string, string> = {},
): ToolCallView[] {
  return (toolCalls ?? []).map((tc, idx) => {
    const toolMsg = resultByCallId.get(tc.id)
    const failure = toolMsg ? failureOf(toolMsg.content) : null
    return {
      callId: tc.id || `tc-${idx}`,
      name: tc.name || '未知工具',
      rawArgs: tc.arguments ?? '',
      // 库里不带 permission：按工具名从注册表清单回填，让徽标与执行时同源
      permission: permByTool[tc.name] ?? '',
      ok: toolMsg ? failure === null : null,
      durationMs: null,
      error: failure,
      result: failure ? null : (toolMsg?.content ?? null),
      truncated: false,
    }
  })
}
