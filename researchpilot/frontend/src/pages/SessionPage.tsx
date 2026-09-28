import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { App as AntdApp, Select, Spin } from 'antd'
import { api, ApiError, streamJob } from '../api/client'
import { liveRunToAgentRun, newLiveRun, reduceLiveRun, type LiveRun } from '../api/liveRun'
import type {
  AgentRun,
  AgentStep,
  Approval,
  BlackboardItem,
  Conversation,
  ConversationDetail,
  Message,
  PlanStep,
  Project,
  StageInfo,
  TaskPlan,
} from '../api/types'
import ApprovalCard from '../components/ApprovalCard'
import PlanCard from '../components/PlanCard'
import RunBlock from '../components/RunBlock'
import ToolCallCard from '../components/ToolCallCard'
import { toolCallViewsOf, type ToolCallView } from '../api/toolViews'
import { UsageLine } from '../components/UsagePanel'
import CommandBar, { placeholderHint, type Command } from '../components/CommandBar'
import { Markdown } from '../components/Markdown'

interface SysEvent {
  id: number
  text: string
}

interface RunWithSteps {
  run: AgentRun
  steps: AgentStep[]
}

/** 会话作业在流里的坐标：stage 固定叫 ``chat``，agent 取 spec id。 */
const CHAT_STAGE = 'chat'
const CHAT_AGENT = 'kernel'

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

  // ── 会话（US-401/402）────────────────────────
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [convId, setConvId] = useState<number | null>(null)
  const [conv, setConv] = useState<ConversationDetail | null>(null)
  const [plan, setPlan] = useState<TaskPlan | null>(null)
  const [chatInput, setChatInput] = useState('')
  const [sending, setSending] = useState(false)
  /** 工具名 → 权限档位：落库消息不带 permission，用 GET /api/tools 回填徽标 */
  const [permByTool, setPermByTool] = useState<Record<string, string>>({})

  // 流事件回调是普通函数（不是渲染期的闭包），拿 state 会读到旧值 —— 用 ref 兜住。
  const liveRef = useRef<LiveRun | null>(null)
  const closeStreamRef = useRef<(() => void) | null>(null)
  const sysSeqRef = useRef(0)
  const convIdRef = useRef<number | null>(null)

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

  /** 选中会话后拉详情与计划；会话被并发删掉时如实降级为空态。 */
  const selectDetail = useCallback(
    async (cid: number) => {
      try {
        const [detail, plans] = await Promise.all([
          api.getConversation(cid),
          api.listTaskPlans(cid),
        ])
        setConv(detail)
        // 列表第一条就是「当前计划」（重规划会产生新行）
        setPlan(plans[0] ?? null)
      } catch (err) {
        setConv(null)
        setPlan(null)
        message.error(err instanceof ApiError ? err.message : '加载会话失败')
      }
    },
    [message],
  )

  const loadConversations = useCallback(
    async (selectId?: number) => {
      try {
        const list = await api.listConversations(projectId)
        setConversations(list)
        const target = selectId ?? list[0]?.id ?? null
        convIdRef.current = target
        setConvId(target)
        if (target !== null) await selectDetail(target)
        else {
          setConv(null)
          setPlan(null)
        }
      } catch (err) {
        message.error(err instanceof ApiError ? err.message : '加载会话列表失败')
      }
    },
    [projectId, selectDetail, message],
  )

  useEffect(() => {
    void load()
    void loadConversations()
  }, [load, loadConversations])

  // 工具清单只为权限徽标服务：失败静默（侧栏另有完整清单）
  useEffect(() => {
    let alive = true
    void api
      .listTools()
      .then((tools) => {
        if (alive) setPermByTool(Object.fromEntries(tools.map((t) => [t.name, t.permission])))
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

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

  /** 会话静默重拉；``withApprovals`` 用于终态对账（危险操作的审批单在库里）。 */
  const reloadConversation = useCallback(
    async (opts?: { withApprovals?: boolean }) => {
      const cid = convIdRef.current
      if (cid === null || Number.isNaN(projectId)) return
      try {
        const tasks = [
          api.getConversation(cid).then(setConv),
          api.listTaskPlans(cid).then((plans) => setPlan(plans[0] ?? null)),
          // 首条消息会触发后端自动命名，列表里的标题要跟着刷新
          api.listConversations(projectId).then(setConversations),
        ]
        if (opts?.withApprovals) tasks.push(api.listApprovals(projectId).then(setApprovals))
        await Promise.all(tasks)
        setUsageToken((n) => n + 1)
      } catch (err) {
        message.error(err instanceof ApiError ? err.message : '刷新会话失败')
      }
    },
    [projectId, message],
  )

  /** 作业收尾：关流 → 提示终态 → 丢弃缓冲 → 按数据库现状对账一次。 */
  const settle = useCallback(
    async (final: LiveRun | null) => {
      closeStreamRef.current?.()
      closeStreamRef.current = null
      if (final?.status === 'failed') {
        pushSysEvent(`作业失败：${final.error ?? '未知错误'}`)
      } else if (final?.status === 'paused') {
        // pauseReason 在 stage.paused 事件里先行到达（job.paused 本身不带）
        if (final.pauseReason === 'cancelled') {
          pushSysEvent('已中止：作业停在最近的检查点，可点「继续」从断点恢复')
        } else if (final.pauseReason === 'tool_permission') {
          pushSysEvent('有危险操作等待你的批准；批准后会自动继续')
        } else {
          pushSysEvent('预算熔断，作业已暂停，等待审批')
        }
      }
      applyLive(null)
      if (final?.stageId === CHAT_STAGE) await reloadConversation({ withApprovals: true })
      else await load({ silent: true })
    },
    [applyLive, load, pushSysEvent, reloadConversation],
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
          if (event.type === 'approval.required') {
            pushSysEvent('内核请求批准一次危险操作')
          }
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

  // ── 会话操作 ────────────────────────────────

  const createConversation = async () => {
    try {
      const created = await api.createConversation(projectId)
      await loadConversations(created.id)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '创建会话失败')
    }
  }

  const switchConversation = async (cid: number) => {
    // 会话作业跑着的时候切走，实时缓冲会指到别的会话上 —— 干脆不让切
    if (liveRef.current) return
    convIdRef.current = cid
    setConvId(cid)
    await selectDetail(cid)
  }

  const sendChat = async () => {
    const text = chatInput.trim()
    const cid = convIdRef.current
    if (!text || cid === null || liveRef.current) return
    setSending(true)
    try {
      // D14：202 只承诺「已受理」。先重拉拿回自己那条消息，再订作业流看进展。
      const accepted = await api.sendMessage(cid, text)
      setChatInput('')
      await reloadConversation()
      if (accepted.job_id !== null) watchJob(accepted.job_id, CHAT_STAGE, CHAT_AGENT)
      else pushSysEvent('内核未装配：消息已保存，但没有触发运行')
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '发送失败')
    } finally {
      setSending(false)
    }
  }

  /** 停止（US-407）：202 只代表「已请求」，作业在下一个安全点收口成 paused。 */
  const stopChat = async () => {
    const cur = liveRef.current
    if (!cur) return
    try {
      await api.cancelJob(cur.jobId)
      pushSysEvent('已请求停止；作业会在下一个安全点收口，稍后自动刷新')
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '停止失败')
    }
  }

  /** 续跑（US-407）：新的一次运行，不追加用户消息。不能续时后端会给出原因。 */
  const resumeChat = async () => {
    const cid = convIdRef.current
    if (cid === null || liveRef.current) return
    try {
      const accepted = await api.resumeConversation(cid)
      pushSysEvent(`续跑受理：从步骤 ${accepted.resume_from ?? '（无计划）'} 继续`)
      watchJob(accepted.job_id, CHAT_STAGE, CHAT_AGENT)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '续跑失败')
    }
  }

  const approvePlan = async () => {
    if (!plan || !('version' in plan)) return
    try {
      setPlan(await api.updateTaskPlan(plan.id, { status: 'approved' }))
      pushSysEvent('计划已批准并冻结')
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '批准失败')
    }
  }

  /** 只改标题；status 省略 → 后端沿用既有状态，不会把已完成的步骤抹掉。 */
  const savePlanSteps = async (steps: Omit<PlanStep, 'status'>[]) => {
    if (!plan || !('version' in plan)) return
    try {
      setPlan(await api.updateTaskPlan(plan.id, { steps }))
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '保存计划失败')
    }
  }

  const handleApproval = async (approvalId: number, action: 'approve' | 'reject') => {
    try {
      const result = await api.decideApproval(approvalId, action)
      if (action === 'approve' && result.resume_error) {
        pushSysEvent(`批准已生效，但恢复执行失败：${result.resume_error}`)
      }
      // 危险/会话预算批准会派一个**新的** chat 作业恢复执行 —— 立刻接上流
      if (action === 'approve' && result.job_id) {
        watchJob(result.job_id, CHAT_STAGE, CHAT_AGENT)
      }
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '审批操作失败')
    }
    await load({ silent: true })
    await reloadConversation()
  }

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

  // ── 渲染期派生 ───────────────────────────────

  /** role=tool 的结果按 tool_call_id 归位到发起它的调用卡上。 */
  const resultByCallId = useMemo(() => {
    const map = new Map<string, Message>()
    for (const m of conv?.messages ?? []) {
      if (m.role === 'tool' && m.tool_call_id) map.set(m.tool_call_id, m)
    }
    return map
  }, [conv])

  /** 助手消息声明过的调用 id：剩下的 tool 消息就是配不上对的孤儿。 */
  const knownCallIds = useMemo(() => {
    const set = new Set<string>()
    for (const m of conv?.messages ?? []) {
      for (const tc of m.tool_calls ?? []) if (tc.id) set.add(tc.id)
    }
    return set
  }, [conv])

  const liveToToolViews = (calls: LiveRun['toolCalls']): ToolCallView[] =>
    calls.map((c) => ({
      callId: c.callId,
      name: c.tool,
      rawArgs: JSON.stringify(c.args ?? {}),
      permission: c.permission,
      ok: c.ok,
      durationMs: c.durationMs,
      error: c.error,
      result: c.resultPreview,
      truncated: c.truncated,
    }))

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

  const chatLive = live?.stageId === CHAT_STAGE ? live : null
  // 阶段作业才渲染成运行块；会话作业的内容在上方消息流里分卡呈现
  const stageLive = live && live.stageId !== CHAT_STAGE ? live : null
  const stageLiveRun = stageLive ? liveRunToAgentRun(stageLive, projectId) : null
  const planView: TaskPlan | LiveRun['plan'] | null = chatLive?.plan ?? plan
  const canResume =
    !!plan && !live && (plan.status === 'approved' || plan.status === 'executing')

  return (
    <div className="session">
      <div className="stream">
        <div className="stream-inner">
          {/* ── 会话区 ── */}
          <div className="conv-bar">
            <span className="conv-label">会话</span>
            <Select
              size="small"
              style={{ minWidth: 220 }}
              value={convId ?? undefined}
              placeholder="选择会话"
              disabled={chatLive !== null}
              onChange={(value) => void switchConversation(value as number)}
              options={conversations.map((c) => ({
                value: c.id,
                label: c.title || `会话 #${c.id}`,
              }))}
            />
            <button
              className="btn tiny"
              onClick={createConversation}
              disabled={chatLive !== null}
            >
              ＋ 新会话
            </button>
            {conv && (
              <span className="conv-meta">
                {conv.messages.length} 条消息 · ~{conv.total_tokens} tok
              </span>
            )}
          </div>

          {conv === null ? (
            <div className="hintline">
              {'// 还没有会话。点「＋ 新会话」直接对内核说话，或用下方命令行触发一个阶段。'}
            </div>
          ) : (
            <>
              {conv.messages.map((m) => {
                if (m.role === 'user') {
                  return (
                    <div key={m.id} className="userline">
                      <span className="mark" aria-hidden="true">&gt;</span>
                      <span className="text">{m.content}</span>
                      <span className="ts">
                        {new Date(m.created_at).toLocaleTimeString('zh-CN', { hour12: false })}
                      </span>
                    </div>
                  )
                }
                if (m.role === 'assistant') {
                  return (
                    <div key={m.id} className="assistant-block">
                      {m.content && (
                        <div className="assistant-note">
                          <Markdown text={m.content} />
                        </div>
                      )}
                      {/* D12：模型的话（上）与系统的调用（下）分卡渲染 */}
                      {toolCallViewsOf(m.tool_calls, resultByCallId, permByTool).map((v) => (
                        <ToolCallCard key={v.callId} call={v} />
                      ))}
                    </div>
                  )
                }
                // 孤儿 tool 消息：找不到声明它的 assistant 调用，单独摆出来不吞掉
                if (m.role === 'tool' && m.tool_call_id && !knownCallIds.has(m.tool_call_id)) {
                  return (
                    <div key={m.id} className="tool-card">
                      <div className="tool-head">
                        <span className="glyph" aria-hidden="true">⎿</span>
                        <span className="tool-name">（未配对的结果）</span>
                        <span className="tool-meta">{m.tool_call_id}</span>
                      </div>
                      <details className="tool-result">
                        <summary>结果</summary>
                        <pre className="jsonpre">{m.content}</pre>
                      </details>
                    </div>
                  )
                }
                return null
              })}

              <PlanCard
                plan={planView}
                onApprove={plan && plan.status === 'draft' ? approvePlan : undefined}
                onSaveSteps={plan && plan.status === 'draft' ? savePlanSteps : undefined}
                busy={sending}
              />

              {/* 实时缓冲：只展示落库前的那一段；终态一到就被上面的重拉接管 */}
              {chatLive?.assistantDraft && (
                <div className="assistant-block is-live">
                  <div className="assistant-note">
                    <Markdown text={chatLive.assistantDraft.text} />
                  </div>
                  <div className="hintline">{'// 回答中…（最终以保存的回复为准）'}</div>
                </div>
              )}
              {chatLive &&
                liveToToolViews(chatLive.toolCalls).map((v) => (
                  <ToolCallCard key={`live-${v.callId}`} call={v} />
                ))}
            </>
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

          {/* ── 阶段流水线（原有能力，原样保留） ── */}
          <div className="divider">{'// 阶段流水线'}</div>

          {runs.map(({ run, steps }) => (
            <RunBlock
              key={run.id}
              run={run}
              steps={steps}
              writes={writesFor(run)}
              running={false}
            />
          ))}

          {stageLive && stageLiveRun && (
            <RunBlock
              run={stageLiveRun}
              steps={stageLive.steps}
              writes={[]}
              running={stageLive.status === 'running'}
              jobId={stageLive.jobId}
            />
          )}

          {live?.degraded && (
            <div className="hintline">{'// 实时通道已降级为轮询，进度仍会自动刷新'}</div>
          )}

          {runs.length === 0 && !stageLiveRun && (
            <div className="hintline">
              {'// 还没有运行记录。用下方命令行触发一个阶段，Agent 的每一步都会追加在这里。'}
            </div>
          )}

          <UsageLine projectId={projectId} version={usageToken} />
        </div>
      </div>

      {/* ── 会话输入（仅在有会话时出现） ── */}
      {conv && (
        <div className="chatbar">
          <div className="cmd-inner">
            <div className="cmd-row">
              <span className="mark" aria-hidden="true">❯</span>
              <input
                value={chatInput}
                disabled={sending || chatLive !== null}
                placeholder={
                  chatLive ? '内核正在执行…可先停止再继续对话' : '对内核说点什么（回车发送）'
                }
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) void sendChat()
                }}
                aria-label="会话输入"
              />
              {chatLive ? (
                <button className="btn" onClick={() => void stopChat()}>
                  ■ 停止
                </button>
              ) : canResume ? (
                <button className="btn primary" onClick={() => void resumeChat()}>
                  ▶ 继续
                </button>
              ) : (
                <button
                  className="btn primary"
                  onClick={() => void sendChat()}
                  disabled={sending || convId === null}
                >
                  发送
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      <CommandBar stages={stages} disabled={live !== null} onCommand={handleCommand} />
    </div>
  )
}
