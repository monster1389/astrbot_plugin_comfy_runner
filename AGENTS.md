# AGENTS.md

外部 ComfyUI 的 AstrBot 适配器。AstrBot 侧只承担入口、鉴权与消息收发，绘图能力全部来自外部 ComfyUI 服务。

## 文档分工

| 文件 | 管什么 | 不管什么 |
|---|---|---|
| `AGENTS.md` | 操作方式、编码规范、架构不变量、提交门槛 | 模块目录树、依赖版本、变更流水 |
| `GLOSSARY.md` | 领域词的定义与命名裁决 | 机制描述、类名、默认值 |
| `CHANGELOG.md` | 对使用者有影响的行为、命令、配置变化 | 现状描述 |
| `docs/adr/` | 难逆、不读就反直觉、有替代方案被否掉的决定 | 每次重构都记 |
| 代码 docstring | 接口契约：做什么、参数、返回值、异常 | 实现步骤、历史、跨模块说明 |
| `_conf_schema.json` | 配置项、默认值、可选值 | 语义解释 |

## 操作

容器名 `astrbot`；宿主机本目录与容器内 `/AstrBot/data/plugins/astrbot_plugin_comfy_runner` 是同一份。

```bash
scripts/check.sh                    # ruff + 无注释 + 无 emoji
git config core.hooksPath .githooks # 新 clone 后执行一次
docker restart astrbot              # 改动生效
```

Python 一律在 `astrbot` 容器内执行，宿主机不建虚拟环境、不装包。新增依赖先写进 `requirements.txt` 再安装。

## 编码规范

- 无兜底：错误直接抛出，不静默吞掉，不降级运行。
- 注释只保留 docstring；`#` 注释只允许 `# noqa` 与 `# type: ignore`。
- docstring 中文 Google 风格：模块与类必须有；公共函数与方法必须有，按需写 `Args` / `Returns` / `Raises`；私有函数不强制。
- emoji：日志、docstring、配置描述里不出现；用户可见回复只允许 `✅ ❌ ⚠️ 🚫`。
- 日志一律 `from astrbot.api import logger`。
- 函数签名带完整类型注解；不使用通配导入。
- 不写兼容层与转发方法，调用方直接调用真实对象。
- 行宽 88 列。

## 架构不变量

1. 入口只有一层：`main.py` 只含 `ComfyUIPlugin(Star)`、装饰器方法、依赖装配与转调，业务判断不写在 `main.py`。
2. 依赖单向：入口 → 编排（drawing / delivery）→ 领域（prompting / policy / lora / workflow / injector）→ 基础设施（client / config / paths / protocol）。领域层不 import `astrbot`。
3. 配置只有一个真相：`_conf_schema.json` 是默认值与可选值的唯一来源；代码里的默认值只出现在 `config.py` 取值的那一处，函数签名与 `.get()` 不写字面默认值。
4. 协议字段只有一处：ComfyUI 节点字段名（`text` / `loras.__value__` / `strength` / `clipStrength` / `ParameterBreak` / `steps` / `noise_seed`）集中在 `protocol.py`，只允许注入器与 LoRA 适配代码引用。
5. 路径只有一处：数据目录布局由 `paths.py` 定义，其他模块不用 `__file__` 推导路径。
6. 状态有 owner：锁定、冷却、群策略、失败记录、当前工作流各自只有一个持有者，通过参数传入使用方；业务状态不经过 `event.set_extra` 传递。
7. 发送只有一处：`delivery.py` 是唯一调用 `event.send` / `chain_result` 的模块。
8. 错误只有一个出口：领域层抛异常，入口层统一翻译成一条用户可见错误。
9. 不猜：不二次请求 LLM 补提示词，不在输出节点取不到时扫其他节点，不重试到第二个工作流，不在配置缺失时降级运行。
10. 运行时只写数据目录：插件目录内的文件在运行期不被改写。

## 提交门槛

- 改代码等「动手」；`git commit` 等「提交」。
- 提交前 `scripts/check.sh` 为绿。
- 结论写进本文件或 `GLOSSARY.md` 才生效，留在对话里的不算。
