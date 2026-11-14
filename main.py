"""AstrBot 插件入口：注册命令与钩子，装配依赖。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools

from .comfy_runner import commands
from .comfy_runner.access import AccessController, load_lexicon
from .comfy_runner.api import ComfyUI
from .comfy_runner.commands import CommandContext
from .comfy_runner.config import Config
from .comfy_runner.draw import Drawer
from .comfy_runner.execution import ConcurrencyGate, WorkflowExecutor
from .comfy_runner.plugin_state import WorkflowSelection, prepare_data_dir
from .comfy_runner.registry import build_tools
from .comfy_runner.tools import build_function_tools

PLUGIN_DIR = Path(os.path.dirname(os.path.abspath(__file__)))


class ComfyUIPlugin(Star):
    """外部 ComfyUI 的 AstrBot 适配器。"""

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        """装配配置、访问控制、工作流工具与命令上下文。

        Args:
            context: AstrBot 插件上下文。
            config: 合并了 schema 默认值的配置字典。

        Raises:
            ContractError: 任一启用契约不合法，或任一契约文件无法解析。
        """
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_comfy_runner")
        logger.info(f"[ComfyUI] 数据目录: {self.data_dir}")
        prepare_data_dir(self.data_dir, PLUGIN_DIR)

        self.cfg = Config.from_mapping(config, data_dir=self.data_dir)
        self.selection = WorkflowSelection(config, self.data_dir, self.cfg)
        self.cfg = self.selection.config
        control = self.cfg.control

        self.workflow_tools = build_tools(self.cfg.workflow_dir, PLUGIN_DIR)
        self.api = ComfyUI(self.cfg)
        self.executor = WorkflowExecutor(self.api.client)
        self.gate = ConcurrencyGate(self.cfg.execution.max_concurrency)
        self.function_tools = build_function_tools(
            self.workflow_tools,
            __name__,
            self.executor,
            self.gate,
        )

        self.access = AccessController(
            control,
            load_lexicon(self.cfg.sensitive_words_path),
        )
        self.selection.sync_options(self.api.workflow.list_files())
        self.selection.log_status(self.api.workflow)
        self.drawer = Drawer(self.api, self.access, self.cfg.output_dir)
        self.cmd_ctx = CommandContext(
            self.cfg,
            self.access,
            self.api,
            self.drawer,
            self.selection,
            self.context,
            PLUGIN_DIR,
        )

        word_count = sum(len(words) for words in self.access.lexicon.values())
        logger.info(f"[ComfyUI] 敏感词库已加载: {word_count} 个词条")
        logger.info(
            f"[ComfyUI] 超级管理员: {len(control.admin_ids)} 个 | "
            f"白名单群: {len(control.whitelist_group_ids)} 个"
        )
        tool_names = ", ".join(tool.name for tool in self.function_tools) or "无"
        logger.info(f"[ComfyUI] 启用的工作流工具: {tool_names}")
        logger.info(
            f"[ComfyUI] 执行上限: 并发 {self.gate.limit} | "
            f"超时 {self.cfg.execution.timeout_seconds} 秒 | "
            f"轮询 {self.cfg.execution.poll_interval_seconds} 秒"
        )
        if self.access.lockdown:
            logger.warning("[ComfyUI] 绘图功能全局锁定已启用，仅超级管理员可用")
        logger.info("[ComfyUI] ComfyUI API 初始化成功")

    async def initialize(self) -> None:
        """插件加载完成后注册工作流工具并激活旧工具。"""
        if self.function_tools:
            self.context.add_llm_tools(*self.function_tools)
            logger.info(f"[ComfyUI] 已注册 {len(self.function_tools)} 个工作流工具")
        self.context.activate_llm_tool("comfyui_txt2img")
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
        """列出可用工作流。"""
        async for result in commands.list_workflows(self.cmd_ctx, event):
            yield result

    @comfy.command("check")
    async def cmd_comfy_check(self, event: AstrMessageEvent):
        """检查工具契约。"""
        async for result in commands.check_contracts(self.cmd_ctx, event):
            yield result

    @comfy.command("save")
    async def cmd_comfy_save(self, event: AstrMessageEvent):
        """保存或导入工作流。"""
        async for result in commands.save_workflow(self.cmd_ctx, event):
            yield result

    @filter.command("comfy_use")
    async def cmd_comfy_use(self, event: AstrMessageEvent):
        """切换当前工作流。"""
        async for result in commands.use_workflow(self.cmd_ctx, event):
            yield result

    @filter.command("comfy_add")
    async def cmd_comfy_add(self, event: AstrMessageEvent):
        """绑定或查看步数覆盖。"""
        async for result in commands.add_steps(self.cmd_ctx, event):
            yield result

    @filter.command("当前工作流", aliases=["comfy_current", "当前wf"])
    async def cmd_comfy_current(self, event: AstrMessageEvent):
        """显示当前工作流与节点绑定。"""
        async for result in commands.current_workflow(self.cmd_ctx, event):
            yield result

    @filter.command("测试lora", aliases=["lora_test", "lora测试"])
    async def cmd_lora_test(self, event: AstrMessageEvent):
        """查看 LoRA 清单或预演选择解析。"""
        async for result in commands.lora_test(self.cmd_ctx, event):
            yield result

    @filter.command("重绘", aliases=["重抽", "reroll"])
    async def cmd_reroll(self, event: AstrMessageEvent):
        """按给定提示词重绘一张图。"""
        async for result in commands.paint(
            self.cmd_ctx,
            event,
            direct_send=True,
            strip_at=True,
            usage=(
                "用法: /重绘 <提示词>\n"
                "示例: /重绘 1girl, silver hair, cinematic lighting"
            ),
        ):
            yield result

    @filter.command("画图", aliases=["绘画"])
    async def cmd_paint(self, event: AstrMessageEvent):
        """生成一张图并以合并转发发出。"""
        async for result in commands.paint(self.cmd_ctx, event, direct_send=False):
            yield result

    @filter.command("画图no")
    async def cmd_paint_no(self, event: AstrMessageEvent):
        """生成一张图并直接发出。"""
        async for result in commands.paint(self.cmd_ctx, event, direct_send=True):
            yield result

    @filter.on_llm_request(priority=100)
    async def inject_system_prompt(self, event: AstrMessageEvent, req) -> None:
        """注入绘图 system prompt 与最近的失败状态。"""
        await commands.on_llm_request(self.cmd_ctx, event, req)

    @filter.on_llm_response(priority=70)
    async def _extract_prompt_before_filter(
        self,
        event: AstrMessageEvent,
        resp,
    ) -> None:
        """从 LLM 回复里提取绘图标签。"""
        await commands.on_llm_response(self.cmd_ctx, event, resp)

    @filter.on_decorating_result(priority=99)
    async def _auto_paint_from_llm(self, event: AstrMessageEvent) -> None:
        """多图模式下改写消息链，单图模式下启动异步出图。"""
        await commands.stage_build_chain(self.cmd_ctx, event)

    @filter.on_decorating_result(priority=5)
    async def _cleanup_history_prompts(self, event: AstrMessageEvent) -> None:
        """按配置清理历史里的绘图提示词。"""
        await commands.stage_clean_history(self.cmd_ctx, event)

    @filter.on_decorating_result(priority=10)
    async def _send_multi_image_results(self, event: AstrMessageEvent) -> None:
        """按图片占位标记分组发送。"""
        await commands.stage_send_images(self.cmd_ctx, event)

    @llm_tool(name="comfyui_txt2img")
    async def comfyui_txt2img(
        self,
        event: AstrMessageEvent,
        prompt: str = None,
        text: str = None,
        direct_send: bool = False,
    ) -> MessageEventResult:
        """ComfyUI 文生图工具。"""
        async for result in commands.draw_tool(
            self.cmd_ctx,
            event,
            prompt,
            text,
            direct_send,
        ):
            yield result
