import { useState } from 'react'
import type { StageInfo } from '../api/types'

/** 命令行的一条指令（US-311）。 */
export type Command =
  | { kind: 'stage'; stageId: string }
  | { kind: 'goal'; text: string }
  | { kind: 'unknown'; text: string }

interface CommandBarProps {
  stages: StageInfo[]
  disabled: boolean
  onCommand: (command: Command) => void
}

/** 占位阶段的提示文案（US-307），SessionPage 复用。 */
export function placeholderHint(stage: StageInfo): string {
  return stage.planned_sprint
    ? `占位实现，Sprint ${stage.planned_sprint} 交付`
    : '占位实现，尚未排期'
}

/** 底部命令行：点 chip 或输入 run S1 触发阶段；**直接输入研究目标**即触发 S1（US-311）。 */
export default function CommandBar({ stages, disabled, onCommand }: CommandBarProps) {
  const [value, setValue] = useState('')

  const submit = () => {
    const input = value.trim()
    if (!input || disabled) return
    setValue('')

    // 命令形态：run S1 / S1 / s1 / run s1
    const match = input.match(/^(?:run\s+)?(s\d+)$/i)
    if (match) {
      const stage = stages.find(
        (s) => s.stage_id.toLowerCase() === match[1].toLowerCase(),
      )
      onCommand(
        stage
          ? { kind: 'stage', stageId: stage.stage_id }
          : { kind: 'unknown', text: `未知阶段：${match[1].toUpperCase()}` },
      )
      return
    }

    // 其余输入按「研究目标」处理：入口是「说清楚你想研究什么」，
    // 而不是先背会一条命令。
    onCommand({ kind: 'goal', text: input })
  }

  return (
    <div className="cmdbar">
      <div className="cmd-inner">
        <div className="cmd-chips" role="listbox" aria-label="阶段建议">
          {stages.map((s) => (
            <button
              key={s.stage_id}
              className={s.implemented ? 'chip' : 'chip chip-placeholder'}
              disabled={disabled || !s.implemented}
              onClick={() => onCommand({ kind: 'stage', stageId: s.stage_id })}
              title={s.implemented ? s.description : placeholderHint(s)}
            >
              {s.stage_id} {s.name}
            </button>
          ))}
        </div>
        <div className="cmd-row">
          <span className="mark" aria-hidden="true">❯</span>
          <input
            value={value}
            disabled={disabled}
            placeholder="输入研究目标直接开跑，或 run S1（回车执行）"
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            aria-label="命令输入"
          />
        </div>
      </div>
    </div>
  )
}
