import { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { App as AntdApp, Spin } from 'antd'
import { api, ApiError } from '../api/client'
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
import CommandBar from '../components/CommandBar'

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
  const [runningStage, setRunningStage] = useState<string | null>(null)
  const [sysEvents, setSysEvents] = useState<SysEvent[]>([])
  const [sysSeq, setSysSeq] = useState(0)

  const load = useCallback(async () => {
    if (Number.isNaN(projectId)) return
    setLoading(true)
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
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setNotFound(true)
      else message.error(err instanceof ApiError ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [projectId, message])

  useEffect(() => {
    void load()
  }, [load])

  const handleRun = async (stageId: string) => {
    if (runningStage) return
    if (stageId === '?' || !stages.some((s) => s.stage_id === stageId)) {
      setSysEvents((evts) => [...evts, { id: sysSeq, text: `未知命令，试试：run ${stages[0]?.stage_id ?? 'S1'}` }])
      setSysSeq((n) => n + 1)
      return
    }
    setRunningStage(stageId)
    try {
      await api.runStage(projectId, stageId)
    } catch {
      // 阶段失败时后端记录 failed run，流内会显示 ✗ 与错误信息
      setSysEvents((evts) => [...evts, { id: sysSeq, text: `${stageId} 运行失败，详见流内记录` }])
      setSysSeq((n) => n + 1)
    } finally {
      setRunningStage(null)
      await load()
    }
  }

  const handleApproval = async (approvalId: number, action: 'approve' | 'reject') => {
    try {
      await api.decideApproval(approvalId, action)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '审批操作失败')
    }
    await load()
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

          {runningStage && (
            <RunBlock
              run={{
                id: -1,
                project_id: projectId,
                stage_id: runningStage,
                agent_id: stages.find((s) => s.stage_id === runningStage)?.agent_id ?? '',
                status: 'running',
                steps: 0,
                error: null,
                started_at: new Date().toISOString(),
                finished_at: null,
              }}
              steps={[]}
              writes={[]}
              running
            />
          )}

          {runs.length === 0 && !runningStage && (
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
        </div>
      </div>

      <CommandBar stages={stages} disabled={runningStage !== null} onRun={handleRun} />
    </div>
  )
}
