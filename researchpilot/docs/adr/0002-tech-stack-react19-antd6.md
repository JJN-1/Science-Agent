# ADR-0002: 前端技术栈取 React 19 + Ant Design 6

- 状态：已采纳
- 日期：2026-09-19
- 关联：设计文档 §4.1 界面层；FIX-11

## 背景

设计文档原写「React 18 + TS + Vite + Ant Design 5」。
项目自 Sprint 1 起从零搭建，没有任何既有代码或第三方组件依赖旧版本。

## 决策

> 我们决定前端采用 React 19 + Ant Design 6 + Vite 8 + TypeScript 6，
> 并以 oxlint 承担静态检查。

## 理由

- 新项目无迁移成本，直接落在各依赖的当前稳定版，避免开局即欠债。
- AntD 6 与 React 19 为同期版本，官方兼容，无需适配层。
- React 19 起 `ref` 可作为普通 prop、`useSyncExternalStore` 等能力完善，组件写法更直接。
- oxlint 冷启动与增量检查显著快于 ESLint 插件链，适合在 CI 中作为第一道快速闸口。

## 与设计文档的差异

| 设计文档 | 实际采用 |
|---|---|
| React 18 | React 19 |
| Ant Design 5 | Ant Design 6 |
| （未指定 lint 工具） | oxlint |

## 后果

### 正面

- 不受旧版本 API 兼容约束，组件可直接使用新语义。
- 校验链路快，`lint` 与 `build` 可在本地一次跑完。

### 负面 / 代价

- 生态中部分第三方组件仍以 React 18 / AntD 5 为基线，引入前需逐个确认兼容性。
- React 19 对 effect 内同步 `setState` 的约束更严，相关写法会触发 lint 提示（当前 `SettingsDialog` 存在已知告警，不影响构建）。
- AntD 6 主题 token 与 v5 不同名，样式定制需以 v6 token 为准。

### 需要跟随的动作

- 引入任何 UI 依赖前，确认其 peer 依赖同时覆盖 React 19 与 AntD 6。
- 逐步消化 effect 内同步 `setState` 的既有告警。
