import { Route, Routes, useLocation } from 'react-router-dom'
import Sidebar from './components/Sidebar'
import SessionPage from './pages/SessionPage'
import WelcomePage from './pages/WelcomePage'

function crumb(pathname: string): string {
  const parts = pathname.split('/').filter(Boolean)
  if (parts.length === 0) return '~'
  return '~/' + parts.join('/')
}

export default function App() {
  const { pathname } = useLocation()
  return (
    <div className="shell">
      <header className="topbar">
        <span className="brand">researchpilot</span>
        <span className="path">{crumb(pathname)}</span>
        <span className="spacer" />
        <span className="ver">科研全链路多 Agent · v0.1.0</span>
      </header>
      <Sidebar />
      <Routes>
        <Route path="/" element={<WelcomePage />} />
        <Route path="/projects/:id" element={<SessionPage />} />
      </Routes>
    </div>
  )
}
