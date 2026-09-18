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
          在底部命令行输入 <code>run S1</code> 触发「选题发现」阶段，Agent 的思考与决策会以转录形式实时追加到流里；
          产出的黑板对象作为折叠输出内联在对应运行块下。
        </p>
        <p style={{ marginTop: 14 }}>
          从左侧选择一个项目开始，或{' '}
          <Link to="/" onClick={(e) => e.preventDefault()}>
            新建一个项目
          </Link>
          （左侧栏底部按钮）。
        </p>
        <div className="hintline" style={{ marginTop: 18 }}>
          {'// 阶段 Agent 目前为占位实现（Sprint 1），Sprint 3 起逐个替换为真实 Agent。'}
        </div>
      </div>
    </div>
  )
}
