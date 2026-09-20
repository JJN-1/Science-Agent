import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { App as AntdApp, Spin } from 'antd'
import { api, ApiError, streamJob } from '../api/client'
import { liveRunToAgentRun, newLiveRun, reduceLiveRun, type LiveRun } from '../api/liveRun'
import type {
  AgentRun,
  AgentStep,
  Approval,
  BlackboardItem,
  Project,
  StageInfo,
} from '../api/types'
import ApprovalCard from '../components/ApprovalCard'
import RunBlock from '../components/RunBlock'
import { UsageLine } from '../components/UsagePanel'
import CommandBar, { placeholderHint, type Command } from '../components/CommandBar'

interface SysEvent {
  id: number
  text: string
}

interface RunWithSteps {
  run: AgentRun
  steps: AgentStep[]
}

export default function SessionPage() {
  const { message } = AntdApp.useApp()
  const { id } = useParams()
  const projectId = Number(id)
  const [project, setProject] = useState<Project | null>(null)
  const [stages, setStages] = useState<StageInfo[]>([])
  const [runs, setRuns] = useState<RunWithSteps[]>([])
  const [blackboard, setBlackboard] = useState<BlackboardItem[]>([])
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [loading, setLoading] = useState(true)
  const [notFound, setNotFound] = useState(false)
  const [live, setLive] = useState<LiveRun | null>(null)
  const [sysEvents, setSysEvents] = useState<SysEvent[]>([])
  // 用量摘要的刷新信号：作业失败不一定多出一条 run，用它才追得上失败调用。
  const [usageToken, setUsageToken] = useState(0)

  // 流事件回调是普通函数（不是渲染期的闭包），拿 state 会读到旧值 —— 用 ref 兜住。
  const liveRef = useRef<LiveRun | null>(null)
  const closeStreamRef = useRef<(() => void) | null>(null)
  const sysSeqRef = useRef(0)

  /** ``silent`` 用于作业跑完后的对账刷新：不翻整页 loading，只悄悄换掉快照。 */
  const load = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (Number.isNaN(projectId)) return
      if (!opts?.silent) setLoading(true)
      try {
        const p = await api.getProject(projectId)
        setProject(p)
        const [s, r, b, ap] = await Promise.all([
          api.listStages(),
          api.listRuns(projectId),
          api.getBlackboard(projectId),
          api.listApprovals(projectId),
        ])
        setStages(s)
        setBlackboard(b)
        setApprovals(ap)
        // 每个 run 拉取完整步骤（Sprint 1 规模小，可接受），按时间正序呈现在会话流中
        const withSteps = await Promise.all(
          r.map(async (run) => ({ run, steps: (await api.getRun(run.id)).steps ?? [] })),
        )
        withSteps.sort((a, b) => a.run.id - b.run.id)
        setRuns(withSteps)
        setUsageToken((n) => n + 1)
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) setNotFound(true)
        else message.error(err instanceof ApiError ? err.message : '加载失败')
      } finally {
        if (!opts?.silent) setLoading(false)
      }
    },
    [projectId, message],
  )

  useEffect(() => {
    void load()
  }, [load])

  // 卸载或切项目必须关流，否则连接会跟着页面一直挂着
  useEffect(() => () => closeStreamRef.current?.(), [])

  const pushSysEvent = useCallback((text: string) => {
    sysSeqRef.current += 1
    const id = sysSeqRef.current
    setSysEvents((evts) => [...evts, { id, text }])
  }, [])

  const applyLive = useCallback((next: LiveRun | null) => {
    liveRef.current = next
    setLive(next)
  }, [])

  /** 作业收尾：关流 → 提示终态 → 丢弃缓冲 → 按数据库现状对账一次。 */
  const settle = useCallback(
    async (final: LiveRun | null) => {
      closeStreamRef.current?.()
      closeStreamRef.current = null
      if (final?.status === 'failed') {
        pushSysEvent(`作业失败：${final.error ?? '未知错误'}`)
      } else if (final?.status === 'paused') {
        pushSysEvent('预算熔断，作业已暂停，等待审批')
      }
      applyLive(null)
      await load({ silent: true })
    },
    [applyLive, load, pushSysEvent],
  )

  /** 受理成功 → 挂上事件流。进度从流里来，最终状态从数据库来。 */
  const watchJob = useCallback(
    (jobId: number, stageId: string, agentId: string) => {
      closeStreamRef.current?.()
      applyLive(newLiveRun(jobId, stageId, agentId))
      closeStreamRef.current = streamJob(jobId, {
        onEvent: (event) => {
          const cur = liveRef.current
          if (!cur) return
          const { live: next, done } = reduceLiveRun(cur, event)
          applyLive(next)
          if (done) void settle(next)
        },
        onFallback: () => {
          const cur = liveRef.current
          if (!cur) return
          applyLive({ ...cur, degraded: true })
          pushSysEvent('实时通道不稳定，已降级为每秒轮询')
        },
        onError: (msg) => {
          pushSysEvent(`作业流中断：${msg}`)
          void settle(null)
        },
      })
    },
    [applyLive, pushSysEvent, settle],
  )

  const startStage = useCallback(
    async (stageId: string) => {
      const target = stages.find((s) => s.stage_id === stageId)
      if (!target) {
        pushSysEvent(`未知阶段：${stageId}。可用：${stages.map((s) => s.stage_id).join(' / ')}`)
        return
      }
      // US-307：占位阶段不可直接运行，避免占位实现被当成已有能力
      if (!target.implemented) {
        pushSysEvent(`${target.stage_id} ${target.name}：${placeholderHint(target)}`)
        return
      }
      try {
        // FIX-03：受理接口只承诺「已排队」，所以紧接着要自己去订流，
        // 否则界面会停在「已受理」而看不到任何进展。
        const accepted = await api.runStage(projectId, stageId)
        watchJob(accepted.job_id, stageId, target.agent_id)
      } catch (err) {
        pushSysEvent(err instanceof ApiError ? `受理失败：${err.message}` : '受理失败')
      }
    },
    [projectId, stages, pushSysEvent, watchJob],
  )

  /** 命令行入口（US-311）：自由文本按「研究目标」处理，落到 goal 后直接触发 S1。 */
  const handleCommand = async (command: Command) => {
    if (command.kind === 'unknown') {
      pushSysEvent(command.text)
      return
    }
    if (liveRef.current) return
    if (command.kind === 'goal') {
      const scout = stages.find((s) => s.stage_id === 'S1')
      if (!scout?.implemented) {
        pushSysEvent('S1 选题发现尚不可用，暂时无法从研究目标入手')
        return
      }
      try {
        // 先落 goal 再跑：S1 的提示词读的就是 project.goal，
        // 顺序反了这一轮就跑在旧目标上。
        await api.updateProject(projectId, { goal: command.text })
        setProject((p) => (p ? { ...p, goal: command.text } : p))
      } catch (err) {
        pushSysEvent(err instanceof ApiError ? err.message : '更新研究目标失败')
        return
      }
      await startStage('S1')
      return
    }
    await startStage(command.stageId)
  }

  const handleApproval = async (approvalId: number, action: 'approve' | 'reject') => {
    try {
      await api.decideApproval(approvalId, action)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '审批操作失败')
    }
    await load({ silent: true })
  }

  if (notFound) {
    return (
      <div className="session">
        <div className="stream">
          <div className="hintline">// 项目不存在</div>
        </div>
      </div>
    )
  }
  if (loading || !project) {
    return (
      <div className="session">
        <div className="stream" style={{ textAlign: 'center', paddingTop: 64 }}>
          <Spin />
        </div>
      </div>
    )
  }

  // 每个 run 期间由其 agent 写入的黑板对象（按 produced_by + 时间窗匹配）
  const writesFor = (run: AgentRun) =>
    blackboard.filter(
      (w) =>
        w.produced_by === run.agent_id &&
        w.created_at >= run.started_at &&
        (!run.finished_at || w.created_at <= run.finished_at),
    )

  const liveRun = live ? liveRunToAgentRun(live, projectId) : null

  return (
    <div className="session">
      <div className="stream">
        <div className="stream-inner">
          <div className="userline">
            <span className="mark" aria-hidden="true">&gt;</span>
            <span className="text">{project.goal || project.title}</span>
            <span className="ts">{new Date(project.created_at).toLocaleDateString('zh-CN')}</span>
          </div>

          {runs.map(({ run, steps }) => (
            <RunBlock
              key={run.id}
              run={run}
              steps={steps}
              writes={writesFor(run)}
              running={false}
            />
          ))}

          {liveRun && live && (
            <RunBlock
              run={liveRun}
              steps={live.steps}
              writes={[]}
              running={live.status === 'running'}
              jobId={live.jobId}
            />
          )}

          {live?.degraded && (
            <div className="hintline">{'// 实时通道已降级为轮询，进度仍会自动刷新'}</div>
          )}

          {runs.length === 0 && !liveRun && (
            <div className="hintline">
              {'// 还没有运行记录。用下方命令行触发一个阶段，Agent 的每一步都会追加在这里。'}
            </div>
          )}

          {sysEvents.map((evt) => (
            <div key={evt.id} className="sysline">
              <span className="glyph" aria-hidden="true">!</span>
              <span>{evt.text}</span>
            </div>
          ))}

          {approvals.map((ap) => (
            <ApprovalCard
              key={ap.id}
              approval={ap}
              onDecide={(action) => handleApproval(ap.id, action)}
            />
          ))}

          <UsageLine projectId={projectId} version={usageToken} />
        </div>
      </div>

      <CommandBar stages={stages} disabled={live !== null} onCommand={handleCommand} />
    </div>
  )
}
