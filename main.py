"""AstrBot 插件入口：注册工作流工具与管理命令，装配依赖。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .comfy_runner import commands
from .comfy_runner.access import AccessController, load_lexicon
from .comfy_runner.background import BackgroundJobs
from .comfy_runner.comfy import ComfyClient
from .comfy_runner.commands import CommandContext
from .comfy_runner.config import Config
from .comfy_runner.execution import ConcurrencyGate, WorkflowExecutor
from .comfy_runner.interactive import InteractiveManager
from .comfy_runner.paths import PLUGIN_DIR, prepare_data_dir
from .comfy_runner.registry import load_contracts
from .comfy_runner.tools import (
    EXECUTE_NAME,
    INSPECT_NAME,
    LIST_NAME,
    WorkflowToolAdapter,
    build_function_tools,
)
from .comfy_runner.web import WebController


class ComfyUIPlugin(Star):
    """外部 ComfyUI 的 AstrBot 适配器。"""

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        """装配配置、访问控制、工作流工具与命令上下文。

        Args:
            context: AstrBot 插件上下文。
            config: 合并了 schema 默认值的配置字典。

        Raises:
            ConfigError: 配置项缺失、类型不符或取值非法。
            ContractError: 任一契约文件不合法或无法解析。
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
        self.jobs = BackgroundJobs()
        self.contracts = load_contracts(self.cfg.workflow_dir, PLUGIN_DIR)
        self.model_contracts = [
            contract for contract in self.contracts if contract.model_visible
        ]
        self.adapter = WorkflowToolAdapter(
            self.access,
            WorkflowExecutor(self.client),
            self.gate,
            self.jobs,
            {contract.name: contract for contract in self.model_contracts},
            self.cfg.output_dir,
        )
        self.interactive = InteractiveManager(
            self.adapter,
            execution.interactive_timeout_seconds,
        )
        self.function_tools = build_function_tools(self.adapter, __name__)
        self.cmd_ctx = CommandContext(
            self.cfg,
            self.access,
            self.client,
            PLUGIN_DIR,
            tuple(self.contracts),
            self.interactive,
        )
        self.web = WebController(self)
        if hasattr(self.context, "register_web_api"):
            self.web.register(self.context)

        word_count = sum(len(words) for words in self.access.lexicon.values())
        logger.info(
            f"[ComfyUI] 敏感词库: {word_count} 个词条 | "
            f"管理员: {len(self.access.admin_ids)} 个 | "
            f"白名单: {len(self.access.whitelist_ids)} 条"
        )
        workflow_names = ", ".join(self.cmd_ctx.workflow_names) or "无"
        model_names = ", ".join(c.name for c in self.model_contracts) or "无"
        logger.info(
            f"[ComfyUI] 可用工作流: {workflow_names} | 模型可见: {model_names}"
        )
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

    def reload_contracts(self) -> tuple[str, ...]:
        """重新读取并校验全部工具契约，在内存中刷新运行状态。

        Returns:
            当前全部可用工作流名。

        Raises:
            ContractError: 任一契约文件不可读、不符合 Schema 或语义不成立。
        """
        contracts = load_contracts(self.cfg.workflow_dir, PLUGIN_DIR)
        model_contracts = [
            contract for contract in contracts if contract.model_visible
        ]
        self.contracts = contracts
        self.model_contracts = model_contracts
        self.adapter.catalog.clear()
        self.adapter.catalog.update(
            {contract.name: contract for contract in model_contracts}
        )
        names = tuple(contract.name for contract in contracts)
        self.cmd_ctx = replace(self.cmd_ctx, contracts=tuple(contracts))
        self._sync_tool_registration(bool(model_contracts))
        logger.info(f"[ComfyUI] 工具契约已刷新，当前可用: {', '.join(names) or '无'}")
        return names

    def _sync_tool_registration(self, has_workflows: bool) -> None:
        """在模型可见契约数量跨过零边界时同步注册或移除工作流工具。"""
        if has_workflows and not self.function_tools:
            self.function_tools = build_function_tools(self.adapter, __name__)
            self.context.add_llm_tools(*self.function_tools)
            logger.info(f"[ComfyUI] 已注册 {len(self.function_tools)} 个工作流工具")
            return
        if has_workflows or not self.function_tools:
            return
        if hasattr(self.context, "get_llm_tool_manager"):
            manager = self.context.get_llm_tool_manager()
            for name in (LIST_NAME, INSPECT_NAME, EXECUTE_NAME):
                manager.remove_func(name)
        self.function_tools = []
        logger.info("[ComfyUI] 已移除工作流工具")

    async def terminate(self) -> None:
        """插件卸载或重载时取消交互向导与尚未完成的后台交付。"""
        await self.interactive.cancel_all()
        await self.jobs.cancel_all()
        logger.info("[ComfyUI] 插件已卸载")

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

    @comfy.command("run")
    async def cmd_comfy_run(self, event: AstrMessageEvent):
        """手动触发工作流并逐轮收集参数。"""
        async for result in commands.run_workflow_command(self.cmd_ctx, event):
            yield result

    @comfy.command("cancel")
    async def cmd_comfy_cancel(self, event: AstrMessageEvent):
        """取消当前交互参数收集。"""
        async for result in commands.cancel_workflow_command(self.cmd_ctx, event):
            yield result

    @comfy.command("check")
    async def cmd_comfy_check(self, event: AstrMessageEvent):
        """检查工具契约。"""
        async for result in commands.check_contracts(self.cmd_ctx, event):
            yield result

    @comfy.command("reload")
    async def cmd_comfy_reload(self, event: AstrMessageEvent):
        """重新加载工具契约。"""
        async for result in commands.reload_contracts_command(
            self.cmd_ctx,
            self.reload_contracts,
            event,
        ):
            yield result
