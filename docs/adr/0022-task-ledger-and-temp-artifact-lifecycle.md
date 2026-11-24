---
status: accepted
---

# 用 SQLite 任务账本持久化任务状态，并按需下载产物到临时目录

延伸并部分修订 ADR-0008、ADR-0013、ADR-0014、ADR-0015 与 ADR-0017。

只提供 `comfy_execute` 并在后台把产物落成匿名 UUID 文件直接发送给会话，会导致模型在提交后无法获知任务进度、无法识别及串联刚生成的产物，也无法在网络超时后安全补发；同时把全部生成结果永久保存在插件本地会持续浪费宿主存储空间，且 ComfyUI 重启会丢失 `/history` 记录。

因此做出以下决策：

1. **持久化任务账本（`tasks.db`）**：使用标准库 `sqlite3` 在插件数据目录持久化每个任务的 `prompt_id`、工作流名、提交者、关键参数、执行状态、交付状态，以及完成后的正式产物定位（`subfolder`、`filename`、`type`、`output`）。ComfyUI 产物本体永久留在 ComfyUI 服务端，即便 ComfyUI 重启清空 `/history` 或 AstrBot 重启，插件仍可凭账本定位通过 `/view` 拉取或补发历史产物。
2. **新增固定第四工具 `comfy_task`**：公开 `status`（查并发与任务账本）、`wait`（等待完成）、`download`（按需下载到临时目录并返回路径）、`deliver`（发送或补发到当前会话）与 `cancel`（取消排队、执行或依赖等待中的任务并立即归还并发名额）。`/comfy cancel [任务ID]` 亦复用同一取消能力。
3. **临时目录取代永久 `output/`**：不再默认永久保存产物副本。`comfy_execute` 新增可选参数 `deliver`（默认 `true`）；产物仅在发送或显式 `download` 时落盘到 `temp/<subfolder>/<filename>`，沿用 ComfyUI 原始子目录与文件名以支持幂等复用。自动交付或手动 `deliver` 发送成功后立即清理未经 `download` 显式保留的临时文件；显式 `download` 的文件在插件启动与卸载时统一清空。
4. **上游产物引用与无阻塞等待**：文件类参数支持 `task:<prompt_id>[:<序号>]`。当下游引用尚在运行的上游任务时，`comfy_execute` 立即返回并在后台等待上游完成后再拉取上传；而调用 `comfy_task(action="wait")` 时，每秒检查当前会话是否有用户新消息或停止信号，一旦触发立即中断返回，不阻塞对话响应。
5. **排队期不计入执行超时与延迟自愈**：轮询等待期间只要任务仍位于 ComfyUI `queue_pending`，不消耗 `timeout_seconds` 执行超时；超时或状态未知的任务在后续被 `comfy_task` 查询或操作时，若 `/history/{prompt_id}` 已完成则自动修正为已完成。
6. **交付发送重试与状态分离**：消息发送增加有限次重试，发送失败不再掩盖 ComfyUI 执行成功，任务保留已完成且交付失败状态以便后续补发。

## 后果

- 四个固定工具为 `comfy_list`、`comfy_inspect`、`comfy_execute` 与 `comfy_task`，模型无需借助 `shell` 或 `curl` 探测队列与补发消息。
- 跨任务与跨工作流串联不再依赖本地永久落盘文件，仅在模型确实需要读图检查时才按需拉取到 `temp/`。
