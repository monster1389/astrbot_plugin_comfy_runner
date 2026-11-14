"""配置解析：把 AstrBot 合并后的配置字典转成只读对象。"""

from __future__ import annotations

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


def _number(section: Mapping[str, Any], key: str) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"配置项 {key} 必须是数字，实际为 {type(value).__name__}")
    return float(value)


def _boolean(section: Mapping[str, Any], key: str) -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"配置项 {key} 必须是布尔值，实际为 {type(value).__name__}")
    return value


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
class ExecutionSettings:
    """单次执行的边界配置。"""

    max_concurrency: int
    timeout_seconds: int
    poll_interval_seconds: float


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
class Config:
    """插件配置的只读快照。"""

    server_address: str
    execution: ExecutionSettings
    control: ControlSettings
    data_dir: Path

    @property
    def workflow_dir(self) -> Path:
        """工作流文件目录。"""
        return self.data_dir / "workflow"

    @property
    def output_dir(self) -> Path:
        """产物落盘目录。"""
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
        execution = _section(raw, "execution")
        control = _section(raw, "control")
        bypass = _section(control, "admin_bypass")

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
            execution=ExecutionSettings(
                max_concurrency=max_concurrency,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
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
            data_dir=data_dir,
        )
