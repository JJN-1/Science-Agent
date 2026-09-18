import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { App as AntdApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import './index.css'
import App from './App.tsx'

const MONO =
  "ui-monospace, 'Cascadia Code', 'JetBrains Mono', Consolas, 'Courier New', monospace"

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          fontFamily: MONO,
          fontSize: 13,
          borderRadius: 5,
          colorBgContainer: '#ffffff',
          colorBgElevated: '#ffffff',
          colorBorder: '#c8c4b6',
          colorBorderSecondary: '#e2dfd6',
          colorPrimary: '#9a6207',
          colorLink: '#9a6207',
          colorText: '#2b2e26',
          colorTextSecondary: '#6f7466',
        },
      }}
    >
      <AntdApp>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AntdApp>
    </ConfigProvider>
  </StrictMode>,
)
