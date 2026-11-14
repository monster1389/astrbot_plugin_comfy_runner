"""AstrBot 插件入口：注册工作流工具与管理命令，装配依赖。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .comfy_runner import commands
from .comfy_runner.access import AccessController, load_lexicon
from .comfy_runner.comfy import ComfyClient
from .comfy_runner.commands import CommandContext
from .comfy_runner.config import Config
from .comfy_runner.execution import ConcurrencyGate, WorkflowExecutor
from .comfy_runner.paths import prepare_data_dir
from .comfy_runner.registry import build_tools
from .comfy_runner.tools import WorkflowToolAdapter, build_function_tools

PLUGIN_DIR = Path(os.path.dirname(os.path.abspath(__file__)))


class ComfyUIPlugin(Star):
    """外部 ComfyUI 的 AstrBot 适配器。"""

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        """装配配置、访问控制、工作流工具与命令上下文。

        Args:
            context: AstrBot 插件上下文。
            config: 合并了 schema 默认值的配置字典。

        Raises:
            ConfigError: 配置项缺失、类型不符或取值非法。
            ContractError: 任一启用契约不合法，或任一契约文件无法解析。
        """
        super().__init__(context)
        data_dir = StarTools.get_data_dir("astrbot_plugin_comfy_runner")
        logger.info(f"[ComfyUI] 数据目录: {data_dir}")
        prepare_data_dir(data_dir, PLUGIN_DIR)

        self.cfg = Config.from_mapping(config, data_dir=data_dir)
        execution = self.cfg.execution

        self.access = AccessController(
            self.cfg.control,
            load_lexicon(self.cfg.sensitive_words_path),
        )
        self.client = ComfyClient(
            self.cfg.server_address,
            timeout=execution.timeout_seconds,
            poll_interval=execution.poll_interval_seconds,
        )
        self.gate = ConcurrencyGate(execution.max_concurrency)
        self.adapter = WorkflowToolAdapter(
            self.access,
            WorkflowExecutor(self.client),
            self.gate,
            self.cfg.output_dir,
        )
        self.workflow_tools = build_tools(self.cfg.workflow_dir, PLUGIN_DIR)
        self.function_tools = build_function_tools(
            self.workflow_tools,
            __name__,
            self.adapter,
        )
        self.cmd_ctx = CommandContext(
            self.cfg,
            self.access,
            self.client,
            PLUGIN_DIR,
            tuple(tool.name for tool in self.function_tools),
        )

        word_count = sum(len(words) for words in self.access.lexicon.values())
        logger.info(
            f"[ComfyUI] 敏感词库: {word_count} 个词条 | "
            f"管理员: {len(self.access.admin_ids)} 个 | "
            f"白名单群: {len(self.access.whitelist_group_ids)} 个"
        )
        tool_names = ", ".join(self.cmd_ctx.tool_names) or "无"
        logger.info(f"[ComfyUI] 启用的工作流工具: {tool_names}")
        logger.info(
            f"[ComfyUI] 执行上限: 并发 {self.gate.limit} | "
            f"超时 {execution.timeout_seconds} 秒 | "
            f"轮询 {execution.poll_interval_seconds} 秒"
        )
        if self.access.lockdown:
            logger.warning("[ComfyUI] 工作流工具全局锁定已启用，仅管理员可用")
        logger.info("[ComfyUI] 插件装配完成")

    async def initialize(self) -> None:
        """插件加载完成后注册工作流工具。"""
        if self.function_tools:
            self.context.add_llm_tools(*self.function_tools)
            logger.info(f"[ComfyUI] 已注册 {len(self.function_tools)} 个工作流工具")
        logger.info("[ComfyUI] 插件初始化完成")

    @filter.command_group("comfy")
    def comfy(self):
        """ComfyUI 工作流工具命令组。"""

    @comfy.command("help")
    async def cmd_comfy_help(self, event: AstrMessageEvent):
        """显示帮助。"""
        async for result in commands.help_command(self.cmd_ctx, event):
            yield result

    @comfy.command("policy", alias={"banlevel"})
    async def cmd_set_policy(self, event: AstrMessageEvent):
        """查看或设置本群违禁级别。"""
        async for result in commands.set_policy(self.cmd_ctx, event):
            yield result

    @comfy.command("lock")
    async def cmd_comfy_lock(self, event: AstrMessageEvent):
        """切换全局锁定。"""
        async for result in commands.toggle_lockdown(self.cmd_ctx, event):
            yield result

    @comfy.command("ls")
    async def cmd_comfy_list(self, event: AstrMessageEvent):
        """列出工作流与契约状态。"""
        async for result in commands.list_workflows(self.cmd_ctx, event):
            yield result

    @comfy.command("check")
    async def cmd_comfy_check(self, event: AstrMessageEvent):
        """检查工具契约。"""
        async for result in commands.check_contracts(self.cmd_ctx, event):
            yield result

    @comfy.command("save")
    async def cmd_comfy_save(self, event: AstrMessageEvent):
        """导入工作流或契约。"""
        async for result in commands.save_workflow(self.cmd_ctx, event):
            yield result
