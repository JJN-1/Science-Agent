import { useState } from 'react'
import type { PlanStep, TaskPlan } from '../api/types'
import type { LivePlan } from '../api/liveRun'

interface PlanCardProps {
  /** 落库计划（可编辑/可批）或流内实时计划（只读） */
  plan: TaskPlan | LivePlan | null
  /** draft → approved（批准即冻结）。实时计划没有这一步。 */
  onApprove?: () => Promise<void>
  /** 保存人工修改的步骤。省略 status 的步骤沿用既有状态，不会抹掉进度。 */
  onSaveSteps?: (steps: Omit<PlanStep, 'status'>[]) => Promise<void>
  busy?: boolean
}

const STEP_GLYPH: Record<string, string> = {
  pending: '○',
  running: '✻',
  done: '●',
  skipped: '⤼',
  failed: '✗',
}

const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  approved: '已批准',
  executing: '执行中',
  done: '已完成',
  failed: '失败',
  pending: '待执行',
}

function isPersisted(plan: TaskPlan | LivePlan): plan is TaskPlan {
  return 'version' in plan
}

/** 步骤状态行内的徽标色，与 CSS 的 .plan-step.s-* 对应 */
function stepClass(status: string): string {
  return `s-${status}`
}

/**
 * 计划卡片（US-403）：结构化步骤 + 状态徽标；draft 计划可改标题、可批准。
 *
 * 「步骤可编辑」刻意只开放**标题**：改 intent/tool/params 等于改执行语义，
 * 那类改动应该走「重新规划」，而不是在这里悄悄换掉内核要执行的东西。
 */
export default function PlanCard({ plan, onApprove, onSaveSteps, busy }: PlanCardProps) {
  const [editing, setEditing] = useState(false)
  const [titles, setTitles] = useState<Record<string, string>>({})

  if (!plan) return null
  // D7：确定性计划不允许修改步骤（AGENT-PLAN-003），编辑入口只给自由计划的 draft；
  // 批准不受此限 —— draft → approved 对任何计划都合法
  const editable =
    isPersisted(plan) && plan.status === 'draft' && !plan.deterministic && !!onSaveSteps
  const approvable = isPersisted(plan) && plan.status === 'draft' && !!onApprove

  const startEdit = () => {
    setTitles(Object.fromEntries(plan.steps.map((s) => [s.id, s.title])))
    setEditing(true)
  }

  const save = async () => {
    if (!onSaveSteps) return
    await onSaveSteps(plan.steps.map((s) => ({ ...s, title: titles[s.id]?.trim() || s.title })))
    setEditing(false)
  }

  return (
    <div className="plan-card">
      <div className="plan-head">
        <span className="glyph" aria-hidden="true">⧉</span>
        <span className="title">{plan.title || '执行计划'}</span>
        <span className="badge">{plan.mode}</span>
        {isPersisted(plan) && plan.deterministic && <span className="badge">确定性</span>}
        <span className={`badge b-status-${plan.status}`}>{STATUS_LABEL[plan.status] ?? plan.status}</span>
        {isPersisted(plan) && <span className="meta">v{plan.version}</span>}
        <span className="end">
          {editable && !editing && (
            <button className="btn tiny" onClick={startEdit} disabled={busy}>
              ✎ 编辑
            </button>
          )}
          {approvable && !editing && onApprove && (
            <button className="btn tiny primary" onClick={onApprove} disabled={busy}>
              批准
            </button>
          )}
          {editing && (
            <>
              <button className="btn tiny primary" onClick={() => void save()} disabled={busy}>
                保存
              </button>
              <button className="btn tiny" onClick={() => setEditing(false)} disabled={busy}>
                取消
              </button>
            </>
          )}
        </span>
      </div>

      {plan.rationale && !editing && (
        <details className="plan-rationale">
          <summary>规划依据</summary>
          <div className="plan-rationale-body">{plan.rationale}</div>
        </details>
      )}

      <div className="plan-steps">
        {plan.steps.map((step, idx) => (
          <div key={step.id} className={`plan-step ${stepClass(step.status)}`}>
            <span className="glyph" aria-hidden="true">{STEP_GLYPH[step.status] ?? '○'}</span>
            <span className="seq">{idx + 1}</span>
            {editing ? (
              <input
                value={titles[step.id] ?? step.title}
                onChange={(e) => setTitles((t) => ({ ...t, [step.id]: e.target.value }))}
                aria-label={`步骤 ${step.id} 标题`}
              />
            ) : (
              <span className="title" title={step.intent || step.title}>
                {step.title}
              </span>
            )}
            {step.tool && <span className="badge b-tool">⚙ {step.tool}</span>}
            <span className="meta">{step.id}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
