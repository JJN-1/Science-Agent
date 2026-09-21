"""内核工具层（US-404）。

设计 §4.1 把「工具注册表与调用循环」放在内核层；§5.3 的 ``AgentSpec.tools`` 与
§10.1 的权限分级是同一条链的两端 —— **契约声明在 ``specs``，执行收口在 ``registry``**。

模块分工：

- ``base``：``ToolSpec`` / ``ToolResult`` / ``ToolContext`` / ``Tool`` 契约 + 结果截断
- ``registry``：注册表（重复注册、白名单、参数校验、调用与计时、结果截断）
- ``pipeline``：``run_pipeline`` —— 把 S1–S8 确定性编排注册成内核的一个能力（D1）

``pipeline`` 里的编排函数由 ``main`` 注入而不在这里 import：内核层不该反向依赖
``app.orchestration``（调用方向本来就是编排 → 内核）。
"""
