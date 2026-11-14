# 变更记录

## 未发布

### 定位

从固定文生图插件改为 ComfyUI 工作流工具适配器：每个启用的工具契约注册成独立的
LLM 工具，参数与产物由契约声明，插件不注入提示词、不接管对话，只执行与交付。

### 命令

- 命令收敛为 `comfy` 命令组：`/comfy help`、`/comfy ls`、`/comfy check [remote]`、
  `/comfy save`、`/comfy lock`、`/comfy policy`（别名 `banlevel`）
- 删除 `/画图`、`/画图no`、`/重绘`、`/当前工作流`、`/测试lora`、`/comfy帮助`、
  `/违禁级别`、`/comfy_lock`、`/comfy_ls`、`/comfy_save`、`/comfy_use`、`/comfy_add`
- 新增 `/comfy check`：检查全部契约（含禁用）并定位到参数与节点字段；带 `remote`
  时额外用 ComfyUI `/object_info` 核对目标字段

### 配置

- 移除 `workflow_settings`（全局当前工作流与 `bindings`）与 `llm_settings`
  （多图模式、系统提示词、LoRA 控制）
- 新增 `execution`：`max_concurrency`（默认 2）、`timeout_seconds`（默认 300）、
  `poll_interval_seconds`（默认 1.0）
- 保留 `server_address` 与 `control`

### 行为

- 工作流工具按契约执行：每次读原始工作流副本，只覆盖本次显式提供的参数，提交后
  只轮询同一个任务；未映射字段（含 `seed` / `noise_seed`）保持原值，原始文件不被写回
- 执行完成后全部正式产物作为一条消息链发到当前会话，模型只收到一句简短结果
- 工具调用前依次检查准入（全局锁定、群白名单）、敏感词（契约里的自由文本参数）、
  并发名额、冷却；任一未通过都不提交工作流
- 提交错误、执行失败、产物缺失、下载失败、超时与提交结果未知分别报错；超时或
  结果未知不自动重新提交，并发超限直接拒绝且不排队
- 删除 System Prompt 注入、`<pic prompt>` 标签提取、多图自动绘图、对话历史清理
  与 `comfyui_txt2img` 固定工具，代码里只剩一条执行路径
- 工作流列表不再列出 `.tool.json`、`.steps.json`、`.lora.json` 等附属文件
- 启用契约不合法时插件拒绝加载；`enabled: false` 的契约不注册，映射错误不影响加载

## v0.1.0

- `init`：fork 基线，含多工作流切换、LLM 自动绘图、敏感词过滤
