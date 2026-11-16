---
status: accepted
---

# 用模型可见取代启停开关，并以会话向导支持无模型命令执行

随着工作流增多，部分工作流只希望由用户在命令里手动触发，而不希望暴露给大模型工具调用；同时用户希望在命令模式下逐轮输入参数（包含图片与文件）且全过程不经过模型。

因此用单一的 `model_visible` 字段替换原有 `enabled` 字段，而不是保留「是否启用 + 是否模型可见」双布尔开关：凡配有 `.tool.json` 契约的工作流都在命令入口可用，且全部参与加载校验与工具名去重；`model_visible` 仅决定该工作流是否进入 `comfy_list` / `comfy_inspect` / `comfy_execute` 的模型目录。

手动入口 `/comfy run` 通过 AstrBot 的 `SessionWaiter` 挂载以 `(unified_msg_origin, sender_id)` 隔离的 `SessionFilter`，在管线进入插件与 LLM 前拦截该用户的后续输入并 `stop_event()`，从而在不影响群内其他成员对话的前提下完成多轮参数收集，最后复用 `AccessController`、`ConcurrencyGate`、`WorkflowExecutor` 与 `BackgroundJobs` 提交并交付产物。

## 后果

- 不再存在「禁用后允许映射损坏或重名」的草稿态契约，所有 `.tool.json` 必须保持映射成立与名称唯一。
- WebUI 删除工作流与契约不再硬性拦截 `model_visible: true`，仅做确认提示。
- 命令模式下文件类参数只接受消息附件与 `http(s)` 链接，禁止本地绝对路径与 `file://` URI。
