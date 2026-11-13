"""AstrBot 插件入口：注册命令与钩子，装配依赖。"""

from __future__ import annotations

import os
import shutil
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

PLUGIN_DIR = Path(os.path.dirname(os.path.abspath(__file__)))


class ComfyUIPlugin(Star):
    """外部 ComfyUI 的 AstrBot 适配器。"""

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        """装配配置、访问控制、出图器与命令上下文。

        Args:
            context: AstrBot 插件上下文。
            config: 合并了 schema 默认值的配置字典。
        """
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_comfy_runner")
        logger.info(f"[ComfyUI] 数据目录: {self.data_dir}")
        self._init_data_directories()

        self.cfg = Config.from_mapping(config, data_dir=self.data_dir)
        control = self.cfg.control
        llm = self.cfg.llm

        self.access = AccessController(
            control,
            load_lexicon(self.cfg.sensitive_words_path),
        )
        self.api = ComfyUI(self.cfg)
        self.drawer = Drawer(self.api, self.access, self.cfg.output_dir)
        self.cmd_ctx = CommandContext(
            self.cfg,
            self.access,
            self.api,
            self.drawer,
            self.context,
        )

        word_count = sum(len(words) for words in self.access.lexicon.values())
        logger.info(f"[ComfyUI] 敏感词库已加载: {word_count} 个词条")
        logger.info(
            f"[ComfyUI] 超级管理员: {len(control.admin_ids)} 个 | "
            f"白名单群: {len(control.whitelist_group_ids)} 个"
        )
        logger.info(f"[ComfyUI] 多图模式: {'开启' if llm.multi_image_mode else '关闭'}")
        if llm.discard_prompt_from_history:
            logger.info("[ComfyUI] 绘图提示词历史丢弃: 开启")
        if llm.force_draw_when_no_prompt:
            logger.info("[ComfyUI] 强制画图: 开启")
        if llm.target_image_count > 1:
            logger.info(f"[ComfyUI] 目标出图数量: {llm.target_image_count}")
        if self.access.lockdown:
            logger.warning("[ComfyUI] 绘图功能全局锁定已启用，仅超级管理员可用")
        logger.info(
            f"[ComfyUI] 锁定命令开关: "
            f"{'开启' if control.lockdown_command_enabled else '关闭'}"
        )
        logger.info("[ComfyUI] ComfyUI API 初始化成功")

    def _init_data_directories(self) -> None:
        """创建数据目录，首次安装时复制默认敏感词库。

        Raises:
            OSError: 目录或文件无法创建。
        """
        (self.data_dir / "workflow").mkdir(exist_ok=True)
        (self.data_dir / "output").mkdir(exist_ok=True)

        destination = self.data_dir / "sensitive_words.json"
        if destination.exists():
            return
        shutil.copy2(PLUGIN_DIR / "sensitive_words.json", destination)
        logger.info("[ComfyUI] 已复制默认敏感词文件")

    async def initialize(self) -> None:
        """插件加载完成后激活 LLM 工具。"""
        self.context.activate_llm_tool("comfyui_txt2img")
        logger.info("[ComfyUI] 插件初始化完成，LLM 工具已激活")

    @filter.command("comfy帮助")
    async def cmd_comfyui_help(self, event: AstrMessageEvent):
        """显示帮助。"""
        async for result in commands.help_command(self.cmd_ctx, event):
            yield result

    @filter.command("违禁级别", aliases={"banlevel", "敏感级别"})
    async def cmd_set_policy(self, event: AstrMessageEvent):
        """查看或设置本群违禁级别。"""
        async for result in commands.set_policy(self.cmd_ctx, event):
            yield result

    @filter.command("comfy_lock", aliases=["全局锁定", "锁图", "绘图锁定"])
    async def cmd_comfy_lock(self, event: AstrMessageEvent):
        """切换全局锁定。"""
        async for result in commands.toggle_lockdown(self.cmd_ctx, event):
            yield result

    @filter.command("comfy_ls")
    async def cmd_comfy_list(self, event: AstrMessageEvent):
        """列出可用工作流。"""
        async for result in commands.list_workflows(self.cmd_ctx, event):
            yield result

    @filter.command("comfy_use")
    async def cmd_comfy_use(self, event: AstrMessageEvent):
        """切换当前工作流。"""
        async for result in commands.use_workflow(self.cmd_ctx, event):
            yield result

    @filter.command("comfy_save")
    async def cmd_comfy_save(self, event: AstrMessageEvent):
        """保存或导入工作流。"""
        async for result in commands.save_workflow(self.cmd_ctx, event):
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
