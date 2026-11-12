# AGENTS.md

外部 ComfyUI 的 AstrBot 适配器。AstrBot 侧只承担入口、鉴权与消息收发，绘图能力全部来自外部 ComfyUI 服务。

## 文档分工

| 文件 | 管什么 | 不管什么 |
|---|---|---|
| `AGENTS.md` | 操作方式、编码规范、提交门槛 | 模块目录树、依赖版本、变更流水 |
| `GLOSSARY.md` | 领域词的定义与命名裁决 | 机制描述、类名、默认值 |
| `CHANGELOG.md` | 对使用者有影响的行为、命令、配置变化 | 现状描述 |
| `docs/adr/` | 难逆、不读就反直觉、有真实取舍，三者同时成立才写 | 每次重构都记 |
| 代码 docstring | 接口契约：做什么、参数、返回值、异常 | 实现步骤、历史、跨模块说明 |
| `_conf_schema.json` | 配置项、默认值、可选值 | 语义解释 |

`GLOSSARY.md` 的词条以用户确认过的为准，模型不自行新增。

## 操作

```bash
scripts/check.sh                    # ruff + 无注释 + 无 emoji
git config core.hooksPath .githooks # 新 clone 后执行一次
docker restart astrbot              # 改动生效
```

所有 Python 与检查命令在 `astrbot` 容器内执行，宿主机不建虚拟环境、不装包。新增依赖先写进 `requirements.txt` 再安装。

## 编码规范

- 无兜底：错误直接抛出，不静默吞掉，不降级运行。
- 注释只保留 docstring；`#` 注释只允许 `# noqa` 与 `# type: ignore`。
- docstring 中文 Google 风格：模块与类必须有；公共函数与方法必须有，按需写 `Args` / `Returns` / `Raises`；私有函数不强制。
- emoji：日志、docstring、配置描述里不出现；用户可见回复只允许 `✅ ❌ ⚠️ 🚫`。
- 日志一律 `from astrbot.api import logger`。
- 函数签名带完整类型注解；不使用通配导入。
- 不写兼容层与转发方法，调用方直接调用真实对象。
- 测试只覆盖用户感知的行为：命令输出、消息链、配置生效。不测内部函数与实现步骤。
- 行宽 88 列。

## 提交门槛

- 改代码等「动手」；`git commit` 等「提交」。
- 提交前 `scripts/check.sh` 为绿。
- 结论写进本文件或 `GLOSSARY.md` 才生效，留在对话里的不算。
