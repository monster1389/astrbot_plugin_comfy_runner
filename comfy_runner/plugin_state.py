"""插件状态：数据目录、工作流选择与 WebUI schema。"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .config import Config, ConfigError
from .state import (
    STATE_FILENAME,
    WorkflowState,
    load_state,
    resolve_workflow,
    save_state,
)
from .workflow import Workflow


def prepare_data_dir(data_dir: Path, plugin_dir: Path) -> None:
    """创建数据目录，首次安装时复制默认敏感词库。

    Args:
        data_dir: 插件数据目录。
        plugin_dir: 插件源目录，用于读取默认敏感词库。

    Raises:
        OSError: 目录或文件无法创建。
    """
    (data_dir / "workflow").mkdir(exist_ok=True)
    (data_dir / "output").mkdir(exist_ok=True)

    destination = data_dir / "sensitive_words.json"
    if destination.exists():
        return
    shutil.copy2(plugin_dir / "sensitive_words.json", destination)
    logger.info("[ComfyUI] 已复制默认敏感词文件")


class WorkflowSelection:
    """当前工作流选择的加载、持久化与 WebUI 展示。"""

    def __init__(
        self,
        raw_config: dict[str, Any],
        data_dir: Path,
        config: Config,
    ) -> None:
        """读取 state.json，解决命令选择与 WebUI 配置的冲突。

        Args:
            raw_config: AstrBot 传入的配置对象，带 WebUI 使用的 schema。
            data_dir: 插件数据目录。
            config: 已完成类型校验的配置。
        """
        self.raw_config = raw_config
        self.data_dir = data_dir
        self.config_value = config.workflow.json_file
        self.state = resolve_workflow(
            self.config_value,
            load_state(data_dir / STATE_FILENAME),
        )
        save_state(data_dir / STATE_FILENAME, self.state)
        self.config = replace(
            config,
            workflow=replace(config.workflow, json_file=self.state.workflow),
        )
        raw_config["workflow_settings"]["json_file"] = self.state.workflow

    def sync_options(self, files: list[str]) -> None:
        """把工作流文件名写进 WebUI 下拉选项。

        Args:
            files: 当前可用的工作流文件名。

        Raises:
            ConfigError: 配置对象缺少可供 WebUI 读取的 schema。
        """
        schema = getattr(self.raw_config, "schema", None)
        if not isinstance(schema, dict):
            raise ConfigError("AstrBot 配置对象缺少 schema，无法同步工作流列表")

        target = (
            schema.get("workflow_settings", {})
            .get("items", {})
            .get("json_file")
        )
        if not isinstance(target, dict):
            raise ConfigError("配置 schema 缺少 workflow_settings.items.json_file")

        target["options"] = files
        target["enum"] = files
        logger.info(f"[ComfyUI] WebUI 工作流列表已同步: {len(files)} 个")

    def log_status(self, workflow: Workflow) -> None:
        """记录当前工作流与可用列表，便于从日志定位配置问题。

        Args:
            workflow: 当前工作流。
        """
        files = workflow.list_files()
        logger.info(
            f"[ComfyUI] 可用工作流: {', '.join(files) if files else '无'}",
        )
        if not workflow.filename:
            logger.warning("[ComfyUI] 未配置工作流，请用 /comfy ls 查看")
        elif not workflow.path.exists():
            logger.warning(f"[ComfyUI] 当前工作流不存在: {workflow.filename}")

    def persist(self, filename: str) -> None:
        """保存命令切换后的工作流并同步 WebUI 显示值。

        Args:
            filename: 新的当前工作流文件名。
        """
        self.state = WorkflowState(
            workflow=filename,
            config_value=self.config_value,
        )
        save_state(self.data_dir / STATE_FILENAME, self.state)
        self.raw_config["workflow_settings"]["json_file"] = filename
