import { Link } from 'react-router-dom'

export default function WelcomePage() {
  return (
    <div className="session">
      <div className="welcome">
        <h1>
          researchpilot <span className="accent">›</span> 会话
        </h1>
        <p>这是一个科研全链路多 Agent 系统。每个研究项目是一条会话：</p>
        <p>
          在底部命令行<strong>直接写下你的研究目标</strong>（例如「图神经网络在推荐系统中的可解释性」），
          它会成为这个项目的目标并触发 <code>S1 选题发现</code>；Agent 的思考与决策会以转录形式追加到流里，
          产出的黑板对象作为折叠输出内联在对应运行块下。
        </p>
        <p>
          想单独跑某个阶段，输入 <code>run S1</code> 或点上方阶段 chip。
        </p>
        <p style={{ marginTop: 14 }}>
          从左侧选择一个项目开始，或{' '}
          <Link to="/" onClick={(e) => e.preventDefault()}>
            新建一个项目
          </Link>
          （左侧栏底部按钮）。
        </p>
        <div className="hintline" style={{ marginTop: 18 }}>
          {'// S1 选题发现已接真实模型调用；S2–S8 仍是占位实现，随 Sprint 4 起逐个替换。'}
        </div>
      </div>
    </div>
  )
}
