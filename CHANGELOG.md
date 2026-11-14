# 变更记录

## 未发布

- 命令收敛为 `comfy` 命令组：`/comfy help`、`/comfy ls`、`/comfy check`、`/comfy save`、`/comfy lock`、`/comfy policy`（别名 `banlevel`）；不再提供 `/comfy帮助`、`/违禁级别`、`/comfy_lock`、`/comfy_ls`、`/comfy_save`
- 新增工具契约：`workflow/` 目录下 `name.json` 与 `name.tool.json` 配对，启用的契约在插件加载或重载时注册为独立 LLM 工具，参数由契约声明
- 新增 `/comfy check [remote]`：检查全部契约（含禁用），带 `remote` 时额外用 ComfyUI `/object_info` 核对目标字段
- 启用契约不合法时插件拒绝加载；`enabled: false` 的契约不注册，其映射错误不影响加载
- 工作流列表不再列出 `.tool.json`、`.steps.json`、`.lora.json` 等附属文件
- 工作流工具按契约执行：每次读取原始工作流副本，只覆盖本次显式提供的参数，提交后只轮询同一个任务
- 不再随机改写 `seed` / `noise_seed`、不再读取 `.steps.json`、不再改写工作流里的 LoRA 堆；未映射字段保持原值
- 新增配置 `execution`：`max_concurrency`（默认 2）、`timeout_seconds`（默认 300）、`poll_interval_seconds`（默认 1），并发超限直接拒绝且不排队
- 提交错误、执行失败、产物缺失、下载失败、超时与提交结果未知分别报错；超时或结果未知不自动重新提交
- 工作流工具执行完成后，全部正式产物作为一条消息链直接发到当前会话，模型只收到「已生成 N 个产物」的简短结果
- 工具调用前依次检查准入（全局锁定、群白名单）、敏感词（契约里的自由文本参数）、并发名额、冷却；任一未通过都不提交工作流，拒绝原因回给模型
- 删除 System Prompt 注入、`<pic prompt>` 标签提取、多图自动绘图与对话历史清理钩子
- 删除 `/comfy_use`、`/当前工作流`、`/comfy_add`、`/测试lora`；`/comfy ls` 改为展示工作流与契约的配对、启用与校验状态

- WebUI 的工作流下拉仍在：插件加载或重载时列出数据目录里的工作流，`/comfy save` 导入后立即出现
- 旧 `/画图` 在工作流未配置或文件缺失时仍给出明确错误并提示 `/comfy ls`
- 删除 `/comfy_probe_send`、`/comfy_test_send2`、`/api_test_all` 三条调试命令
- 出错提示统一为一套文案；命令回复里的装饰字符与分隔线去掉
- 节点 ID 配置仍为 `workflow_settings.bindings`，仅在旧出图路径生效，随旧路径一起移除

## v0.1.0

- `init`：fork 基线，含多工作流切换、LLM 自动绘图、敏感词过滤
