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

- WebUI 配置的工作流下拉恢复：插件加载/重载时列出数据目录里的工作流，`/comfy_save` 导入后立即出现
- `/comfy_use` 的选择写入数据目录 `state.json`，重启后保持；WebUI 修改配置后以 WebUI 为准
- 工作流未配置或文件缺失时，`/画图` 给出明确错误并提示 `/comfy_ls`
- 删除 `/comfy_probe_send`、`/comfy_test_send2`、`/api_test_all` 三条调试命令
- 出错提示统一为一套文案；命令回复里的装饰字符与分隔线去掉
- `/当前工作流` 修好：此前读的配置键不存在，永远显示"未配置"
- `/测试lora` 输出重写，子命令不变
- 节点 ID 配置从三个字段合并为 `workflow_settings.bindings`：按工作流文件名登记，切换工作流自动套用；旧字段不再读取，需手动抄进新字段
- `/comfy_use` 只接受序号，不再接受节点 ID 参数
- `/comfy_ls` 给已登记绑定的工作流加 `[绑定]` 标记；`/当前工作流` 显示绑定内容或「未登记」提示

## v0.1.0

- `init`：fork 基线，含多工作流切换、LLM 自动绘图、敏感词过滤
