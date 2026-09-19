import { useEffect, useRef, useState } from 'react'
import type { AgentRun, AgentStep, BlackboardItem } from '../api/types'
import { Markdown } from './Markdown'

const KIND_LABEL: Record<string, string> = {
  thought: 'thought',
  tool: 'tool',
  result: 'result',
  decision: 'decision',
}

function timeOf(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? ''
    : d.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' })
}

function stepText(step: AgentStep): string | null {
  const keys = Object.keys(step.content)
  return keys.length === 1 && typeof step.content.text === 'string'
    ? step.content.text
    : null
}

function isLlmCall(step: AgentStep): boolean {
  return step.kind === 'llm_call' && typeof step.content.cost === 'number'
}

/** llm_call 步骤元信息行：⎿ llm plan · model · tok · ¥cost · 降级标记 */
function LlmMeta({ step }: { step: AgentStep }) {
  const c = step.content as Record<string, unknown>
  const prompt = Number(c.prompt_tokens ?? 0)
  const completion = Number(c.completion_tokens ?? 0)
  const cost = Number(c.cost ?? 0)
  const degraded = Array.isArray(c.degraded) ? (c.degraded as string[]) : []
  const flags = [
    c.cached === true ? 'cached' : null,
    ...degraded,
  ].filter(Boolean) as string[]
  return (
    <span className="llm-meta">
      {String(c.tier ?? '')} · {String(c.provider ?? '')}/{String(c.model ?? '')} ·{' '}
      {prompt + completion} tok · ¥{cost.toFixed(4)}
      {flags.length > 0 && <em className="llm-flags"> · ⚠ {flags.join(' / ')}</em>}
    </span>
  )
}

interface RunBlockProps {
  run: AgentRun
  steps: AgentStep[]
  writes: BlackboardItem[]
  running: boolean
}

/** 会话流中的一个运行块：⏺ 头行 + ⎿ 嵌套输出 + 内联黑板写入折叠 + 内联回放 */
export default function RunBlock({ run, steps, writes, running }: RunBlockProps) {
  const [reveal, setReveal] = useState<number | null>(null)
  const [replaying, setReplaying] = useState(false)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [])

  const startReplay = () => {
    if (replaying || steps.length === 0) return
    if (timerRef.current) clearInterval(timerRef.current)
    setReplaying(true)
    setReveal(1)
    let idx = 1
    timerRef.current = setInterval(() => {
      idx += 1
      if (idx > steps.length) {
        if (timerRef.current) clearInterval(timerRef.current)
        timerRef.current = null
        setReplaying(false)
        setReveal(null)
        return
      }
      setReveal(idx)
    }, 900)
  }

  const status = running ? 'running' : run.status
  const runCost = steps.filter(isLlmCall).reduce((sum, s) => sum + Number(s.content.cost), 0)

  return (
    <div>
      <div className={`runhead s-${status}`}>
        <span className="glyph" aria-hidden="true">
          {running ? '✻' : status === 'paused' ? '⏸' : status === 'failed' ? '✗' : '⏺'}
        </span>
        <span className="title">{run.stage_id}</span>
        <span className="meta">{run.agent_id}</span>
        {!running && <span className="meta">· {steps.length} steps</span>}
        {!running && runCost > 0 && (
          <span className="meta run-cost">· ¥{runCost.toFixed(4)}</span>
        )}
        <span className="end">
          <span className="meta">{timeOf(running ? null : run.started_at)}</span>
          {!running && steps.length > 0 && (
            <button className="btn tiny" onClick={startReplay} disabled={replaying}>
              {replaying ? `replay ${reveal}/${steps.length}` : '▶ replay'}
            </button>
          )}
        </span>
      </div>

      {running && (
        <div className="subline">
          <span className="glyph" aria-hidden="true">⎿</span>
          <span className="body" style={{ color: 'var(--accent)' }}>运行中…</span>
        </div>
      )}

      {steps.map((step, idx) => {
        const text = stepText(step)
        const isPending = reveal !== null && idx >= reveal
        const cls = [
          'subline',
          `k-${step.kind}`,
          isPending ? 'is-pending' : '',
          reveal !== null && idx === reveal - 1 ? 'is-current' : '',
        ]
          .filter(Boolean)
          .join(' ')
        return (
          <div key={step.id} className={cls}>
            <span className="glyph" aria-hidden="true">⎿</span>
            <span className="kind">{step.kind === 'llm_call' ? 'llm' : KIND_LABEL[step.kind] ?? step.kind}</span>
            <span className="body">
              {isLlmCall(step) ? (
                <>
                  <LlmMeta step={step} />
                  <details>
                    <summary>模型输出</summary>
                    <pre className="jsonpre">{String(step.content.text ?? '')}</pre>
                  </details>
                </>
              ) : text && text.length <= 120 ? (
                <span className="md-inline">{text}</span>
              ) : text ? (
                <Markdown text={text} />
              ) : (
                <details>
                  <summary>{JSON.stringify(step.content)}</summary>
                  <pre className="jsonpre">{JSON.stringify(step.content, null, 2)}</pre>
                </details>
              )}
            </span>
          </div>
        )
      })}

      {writes.map((w) => (
        <div key={w.id} className="subline k-tool">
          <span className="glyph" aria-hidden="true">⎿</span>
          <span className="body">
            <details>
              <summary>
                → blackboard: {w.obj_type}@v{w.version}
              </summary>
              <pre className="jsonpre">{JSON.stringify(w.payload, null, 2)}</pre>
            </details>
          </span>
        </div>
      ))}

      {run.error && (
        <div className="subline k-result">
          <span className="glyph" aria-hidden="true">⎿</span>
          <span className="body" style={{ color: 'var(--error)' }}>{run.error}</span>
        </div>
      )}
    </div>
  )
}
