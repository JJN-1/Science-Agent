import { useState } from 'react'
import type { Approval } from '../api/types'

interface ApprovalCardProps {
  approval: Approval
  onDecide: (action: 'approve' | 'reject') => Promise<void>
}

/** 流内审批卡片（US-205）：预算/步数熔断时出现在转录流中 */
export default function ApprovalCard({ approval, onDecide }: ApprovalCardProps) {
  const [busy, setBusy] = useState(false)
  const detail = approval.detail
  const message = typeof detail.message === 'string' ? detail.message : ''
  const stage = typeof detail.stage_id === 'string' ? detail.stage_id : ''

  const handle = async (action: 'approve' | 'reject') => {
    setBusy(true)
    try {
      await onDecide(action)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="approval-card" role="alert">
      <div className="approval-head">
        <span className="glyph" aria-hidden="true">⏸</span>
        <span className="title">需要你的确认{stage ? ` · ${stage}` : ''}</span>
        <span className="kind">{approval.kind}</span>
      </div>
      <div className="approval-body">{message || JSON.stringify(detail)}</div>
      <div className="approval-actions">
        <button className="btn primary" disabled={busy} onClick={() => handle('approve')}>
          批准并继续
        </button>
        <button className="btn" disabled={busy} onClick={() => handle('reject')}>
          拒绝
        </button>
      </div>
    </div>
  )
}
