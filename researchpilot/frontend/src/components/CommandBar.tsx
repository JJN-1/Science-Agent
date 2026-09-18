import { useState } from 'react'
import type { StageInfo } from '../api/types'

interface CommandBarProps {
  stages: StageInfo[]
  disabled: boolean
  onRun: (stageId: string) => void
}

/** 底部命令行：输入 run S1 或点阶段建议 chip 触发阶段 */
export default function CommandBar({ stages, disabled, onRun }: CommandBarProps) {
  const [value, setValue] = useState('')

  const submit = () => {
    const input = value.trim()
    if (!input || disabled) return
    // 支持：run S1 / S1 / s1 / run s1
    const match = input.match(/^(?:run\s+)?(s\d+)$/i)
    if (!match) {
      setValue('')
      onRun('?')
      return
    }
    const stage = stages.find((s) => s.stage_id.toLowerCase() === match[1].toLowerCase())
    setValue('')
    onRun(stage ? stage.stage_id : match[1])
  }

  return (
    <div className="cmdbar">
      <div className="cmd-inner">
        <div className="cmd-chips" role="listbox" aria-label="阶段建议">
          {stages.map((s) => (
            <button
              key={s.stage_id}
              className="chip"
              disabled={disabled}
              onClick={() => onRun(s.stage_id)}
              title={s.description}
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
            placeholder="输入命令：run S1（回车执行）"
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            aria-label="命令输入"
          />
        </div>
      </div>
    </div>
  )
}
