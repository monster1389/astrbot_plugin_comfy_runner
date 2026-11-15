---
status: accepted
---

# 隐藏字符串参数支持时间模板覆盖

ComfyUI 官方前端在导出 API 格式工作流时，会把 `SaveImage.filename_prefix` 中的 `%date:...%` 固化为导出当天的静态字符串，而 ComfyUI 后端 `/prompt` 接口本身不解析 `%date:...%`。

为了在不修改导出的工作流 `.json` 文件、也不对未声明字段做全局遍历改写的前提下恢复按日/时分秒组织输出路径的能力，允许在工具契约（`.tool.json`）的**隐藏字符串参数**（`visible: false`、`type: "string"`）上声明 `value_template`。

## 后果

- 延伸 [0018-visibility-and-requirement.md](0018-visibility-and-requirement.md)：隐藏参数在未声明 `value_template` 时仍使用工作流原值；声明 `value_template` 时，每次执行由插件按容器本地时间展开 `%date%` / `%date:<format>%` 并写入内存副本的目标字段。
- 可见参数与非字符串参数禁止声明 `value_template`，避免与「工作流原值」及模型入参校验语义混淆。
- 模板格式在契约加载阶段严格校验，出现空格式 `%date:%` 或无有效日期标记时直接拒绝加载，不向 ComfyUI 透传无效占位符。
