import { parseArgsSafe, type ToolCallView } from '../api/toolViews'

interface ToolCallCardProps {
  call: ToolCallView
}

const PERMISSION_LABEL: Record<string, string> = {
  read: '只读',
  execute: '执行',
  dangerous: '危险',
  unknown: '未知',
}

/** 简短一行参数摘要：太长截断，解析失败就显示原文 */
function argsSummary(raw: string): string {
  const args = parseArgsSafe(raw)
  const text = args
    ? Object.entries(args)
        .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
        .join(', ')
    : raw
  if (!text) return ''
  return text.length > 90 ? `${text.slice(0, 90)}…` : text
}

/**
 * 工具调用卡（US-405 / D12）：**模型的话**与**系统执行的工具**分开成卡。
 * 名称 / 参数 / 耗时 / 结果摘要 / 权限徽标；执行中的卡带呼吸标记，
 * 失败的卡带 ✗ 与错误详情折叠。
 */
export default function ToolCallCard({ call }: ToolCallCardProps) {
  const perm = call.permission || 'unknown'
  const running = call.ok === null
  const summary = argsSummary(call.rawArgs)
  const resultText = call.error ?? call.result

  return (
    <div className={`tool-card${running ? ' is-running' : ''}`}>
      <div className="tool-head">
        <span className="glyph" aria-hidden="true">{running ? '✻' : call.ok ? '⚙' : '✗'}</span>
        <span className="tool-name">{call.name}</span>
        <span className={`perm p-${perm}`}>{PERMISSION_LABEL[perm] ?? perm}</span>
        {call.durationMs !== null && (
          <span className="tool-meta">{(call.durationMs / 1000).toFixed(2)}s</span>
        )}
        {running && <span className="tool-meta tool-waiting">执行中…</span>}
        {call.truncated && <span className="tool-meta">结果已截断</span>}
        {!running && !call.ok && call.error && <span className="tool-meta tool-err">失败</span>}
      </div>

      {summary && <div className="tool-args">{summary}</div>}

      {resultText && (
        <details className="tool-result">
          <summary>{call.ok === false ? '错误详情' : '结果'}</summary>
          <pre className="jsonpre">{resultText}</pre>
        </details>
      )}
    </div>
  )
}
