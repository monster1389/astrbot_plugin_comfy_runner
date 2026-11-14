"""配置解析：把 AstrBot 合并后的配置字典转成只读对象。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLICIES = frozenset({"none", "lite", "full"})


class ConfigError(ValueError):
    """配置项缺失、类型不符或取值非法。"""


def _section(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise ConfigError(f"配置项 {key} 必须是对象，实际为 {type(value).__name__}")
    return value


def _text(section: Mapping[str, Any], key: str) -> str:
    value = section.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"配置项 {key} 必须是字符串，实际为 {type(value).__name__}")
    return value


def _integer(section: Mapping[str, Any], key: str) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"配置项 {key} 必须是整数，实际为 {type(value).__name__}")
    return value


def _boolean(section: Mapping[str, Any], key: str) -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"配置项 {key} 必须是布尔值，实际为 {type(value).__name__}")
    return value


def _number(section: Mapping[str, Any], key: str) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"配置项 {key} 必须是数字，实际为 {type(value).__name__}")
    return float(value)


def _id_set(section: Mapping[str, Any], key: str) -> frozenset[str]:
    value = section.get(key)
    if not isinstance(value, list):
        raise ConfigError(f"配置项 {key} 必须是列表，实际为 {type(value).__name__}")
    return frozenset(str(item) for item in value)


def _policy(section: Mapping[str, Any], key: str) -> str:
    value = _text(section, key).lower()
    if value not in POLICIES:
        allowed = "/".join(sorted(POLICIES))
        raise ConfigError(f"配置项 {key} 只能是 {allowed}，实际为 {value}")
    return value


@dataclass(frozen=True, slots=True)
class NodeBinding:
    """一个工作流文件的节点 ID 绑定。"""

    input_node_id: str
    neg_node_id: str
    output_node_id: str


def _binding_field(
    filename: str,
    field: str,
    entry: Mapping[str, Any],
    *,
    required: bool,
) -> str:
    """取单个绑定字段并校验类型。"""
    value = entry.get(field)
    if value is None:
        if required:
            raise ConfigError(f"配置项 bindings 的 {filename} 缺少 {field}")
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConfigError(
            f"配置项 bindings 的 {filename}.{field} 必须是字符串或整数，"
            f"实际为 {type(value).__name__}"
        )
    text = str(value).strip()
    if required and not text:
        raise ConfigError(f"配置项 bindings 的 {filename}.{field} 不能为空")
    return text


def _bindings(section: Mapping[str, Any], key: str) -> dict[str, NodeBinding]:
    """解析按工作流文件名索引的节点绑定 JSON。"""
    raw = _text(section, key)
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置项 {key} 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置项 {key} 必须是 JSON 对象")

    allowed = {"input", "neg", "output"}
    result: dict[str, NodeBinding] = {}
    for filename, entry in data.items():
        if not isinstance(entry, Mapping):
            raise ConfigError(f"配置项 {key} 的 {filename} 必须是对象")
        unknown = sorted(set(entry) - allowed)
        if unknown:
            raise ConfigError(
                f"配置项 {key} 的 {filename} 含未知字段: {', '.join(unknown)}"
            )
        result[filename] = NodeBinding(
            input_node_id=_binding_field(filename, "input", entry, required=True),
            neg_node_id=_binding_field(filename, "neg", entry, required=False),
            output_node_id=_binding_field(filename, "output", entry, required=False),
        )
    return result


@dataclass(frozen=True, slots=True)
class WorkflowSettings:
    """当前工作流与各工作流的节点绑定。"""

    json_file: str
    bindings: dict[str, NodeBinding]


@dataclass(frozen=True, slots=True)
class LoraControlSettings:
    """LoRA 堆控制配置。"""

    enabled: bool
    inject_catalog_into_system_prompt: bool
    inject_selected_prompt_hints: bool
    keep_workflow_defaults_when_selected: bool
    max_lora_count: int
    scan_directories: str
    max_trigger_options_per_lora: int
    trigger_option_preview_length: int


@dataclass(frozen=True, slots=True)
class LlmSettings:
    """LLM 绘图相关配置。"""

    multi_image_mode: bool
    discard_prompt_from_history: bool
    force_draw_when_no_prompt: bool
    target_image_count: int
    system_prompt: str
    lora_control: LoraControlSettings


@dataclass(frozen=True, slots=True)
class AdminBypass:
    """管理员特权设置。"""

    whitelist: bool
    cooldown: bool
    sensitive_words: bool


@dataclass(frozen=True, slots=True)
class ControlSettings:
    """访问与安全控制配置。"""

    cooldown_seconds: int
    admin_ids: frozenset[str]
    whitelist_group_ids: frozenset[str]
    default_group_policy: str
    default_private_policy: str
    lockdown: bool
    lockdown_command_enabled: bool
    admin_bypass: AdminBypass


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    """单次执行的边界配置。"""

    max_concurrency: int
    timeout_seconds: int
    poll_interval_seconds: float


@dataclass(frozen=True, slots=True)
class Config:
    """插件配置的只读快照。"""

    server_address: str
    workflow: WorkflowSettings
    llm: LlmSettings
    control: ControlSettings
    execution: ExecutionSettings
    data_dir: Path

    @property
    def workflow_dir(self) -> Path:
        """工作流文件目录。"""
        return self.data_dir / "workflow"

    @property
    def output_dir(self) -> Path:
        """出图落盘目录。"""
        return self.data_dir / "output"

    @property
    def sensitive_words_path(self) -> Path:
        """敏感词库文件。"""
        return self.data_dir / "sensitive_words.json"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], data_dir: Path) -> Config:
        """从 AstrBot 合并后的配置字典构建配置。

        Args:
            raw: AstrBot 传入的配置字典，已合并 ``_conf_schema.json`` 的默认值。
            data_dir: 插件的持久化数据目录。

        Returns:
            只读配置对象。

        Raises:
            ConfigError: 配置项缺失、类型不符或取值非法。
        """
        workflow = _section(raw, "workflow_settings")
        llm = _section(raw, "llm_settings")
        lora = _section(llm, "lora_control")
        control = _section(raw, "control")
        bypass = _section(control, "admin_bypass")
        execution = _section(raw, "execution")

        target_image_count = _integer(llm, "target_image_count")
        if target_image_count < 1:
            raise ConfigError(
                f"配置项 target_image_count 必须大于 0，实际为 {target_image_count}",
            )

        max_concurrency = _integer(execution, "max_concurrency")
        if max_concurrency < 1:
            raise ConfigError(
                f"配置项 max_concurrency 必须大于 0，实际为 {max_concurrency}",
            )
        timeout_seconds = _integer(execution, "timeout_seconds")
        if timeout_seconds < 1:
            raise ConfigError(
                f"配置项 timeout_seconds 必须大于 0，实际为 {timeout_seconds}",
            )
        poll_interval_seconds = _number(execution, "poll_interval_seconds")
        if poll_interval_seconds <= 0:
            raise ConfigError(
                "配置项 poll_interval_seconds 必须大于 0，"
                f"实际为 {poll_interval_seconds}",
            )

        return cls(
            server_address=_text(raw, "server_address"),
            workflow=WorkflowSettings(
                json_file=_text(workflow, "json_file"),
                bindings=_bindings(workflow, "bindings"),
            ),
            llm=LlmSettings(
                multi_image_mode=_boolean(llm, "multi_image_mode"),
                discard_prompt_from_history=_boolean(
                    llm,
                    "discard_prompt_from_history",
                ),
                force_draw_when_no_prompt=_boolean(llm, "force_draw_when_no_prompt"),
                target_image_count=target_image_count,
                system_prompt=_text(llm, "system_prompt"),
                lora_control=LoraControlSettings(
                    enabled=_boolean(lora, "enabled"),
                    inject_catalog_into_system_prompt=_boolean(
                        lora,
                        "inject_catalog_into_system_prompt",
                    ),
                    inject_selected_prompt_hints=_boolean(
                        lora,
                        "inject_selected_prompt_hints",
                    ),
                    keep_workflow_defaults_when_selected=_boolean(
                        lora,
                        "keep_workflow_defaults_when_selected",
                    ),
                    max_lora_count=_integer(lora, "max_lora_count"),
                    scan_directories=_text(lora, "scan_directories"),
                    max_trigger_options_per_lora=_integer(
                        lora,
                        "max_trigger_options_per_lora",
                    ),
                    trigger_option_preview_length=_integer(
                        lora,
                        "trigger_option_preview_length",
                    ),
                ),
            ),
            control=ControlSettings(
                cooldown_seconds=_integer(control, "cooldown_seconds"),
                admin_ids=_id_set(control, "admin_ids"),
                whitelist_group_ids=_id_set(control, "whitelist_group_ids"),
                default_group_policy=_policy(control, "default_group_policy"),
                default_private_policy=_policy(control, "default_private_policy"),
                lockdown=_boolean(control, "lockdown"),
                lockdown_command_enabled=_boolean(control, "lockdown_command_enabled"),
                admin_bypass=AdminBypass(
                    whitelist=_boolean(bypass, "whitelist"),
                    cooldown=_boolean(bypass, "cooldown"),
                    sensitive_words=_boolean(bypass, "sensitive_words"),
                ),
            ),
            execution=ExecutionSettings(
                max_concurrency=max_concurrency,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            ),
            data_dir=data_dir,
        )
