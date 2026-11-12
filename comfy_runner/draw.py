"""一次出图的编排：检查、生成、落盘与失败记录。"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest

from .access import AccessController
from .prompt import compact_prompt

FAILURE_TTL_SECONDS = 600
FAILURE_LIMIT = 5


class DrawError(RuntimeError):
    """出图失败，文本是给用户看的一句话。"""


class FailureLog:
    """最近几次失败的原因，按会话保存。"""

    def __init__(self) -> None:
        """初始化空的失败记录。"""
        self._records: dict[str, list[dict[str, Any]]] = {}

    def remember(
        self,
        event: AstrMessageEvent,
        reason: str,
        prompt: str | list[str] = "",
        source: str = "绘图",
        append: bool = False,
    ) -> None:
        """记录一次失败。

        Args:
            event: 消息事件，提供会话标识。
            reason: 给用户看的失败原因。
            prompt: 相关提示词，允许是列表。
            source: 失败来源。
            append: 是否追加到已有记录后面。
        """
        origin = getattr(event, "unified_msg_origin", None)
        if not origin:
            return

        reason_text = " ".join(str(reason or "").split())
        if not reason_text:
            return

        now = time.time()
        record = {
            "ts": now,
            "reason": reason_text,
            "prompt": compact_prompt(prompt),
            "source": str(source or "绘图"),
            "user_id": str(event.get_sender_id()),
        }

        records = self._records.get(origin, []) if append else []
        records = [
            item
            for item in records
            if now - float(item.get("ts", 0)) <= FAILURE_TTL_SECONDS
        ]
        records.append(record)
        self._records[origin] = records[-FAILURE_LIMIT:]

        event.set_extra("comfy_last_draw_failure", record)

    def clear(self, event: AstrMessageEvent) -> None:
        """清空某个会话的失败记录。

        Args:
            event: 消息事件。
        """
        origin = getattr(event, "unified_msg_origin", None)
        if origin:
            self._records.pop(origin, None)

    def build_context(self, event: AstrMessageEvent) -> str:
        """把失败记录渲染成一段注入 LLM 的状态说明。

        Args:
            event: 消息事件。

        Returns:
            状态说明，没有记录时为空串。
        """
        origin = getattr(event, "unified_msg_origin", None)
        if not origin:
            return ""

        now = time.time()
        records = [
            item
            for item in self._records.get(origin, [])
            if now - float(item.get("ts", 0)) <= FAILURE_TTL_SECONDS
        ]
        if not records:
            self._records.pop(origin, None)
            return ""

        self._records[origin] = records[-FAILURE_LIMIT:]
        lines = [
            "<comfyui_draw_status>",
            "这是 ComfyUI 插件注入的内部状态，不是用户发言。",
            "最近一次或几次绘图没有生成图片；如果用户追问刚才的画图结果，请明确说明失败，并引用下面的原因。",
        ]

        for index, item in enumerate(records[-FAILURE_LIMIT:], 1):
            stamp = time.strftime(
                "%H:%M:%S",
                time.localtime(float(item.get("ts", now))),
            )
            line = (
                f"{index}. 时间: {stamp}; 来源: {item.get('source', '绘图')}; "
                f"用户ID: {item.get('user_id', '')}; 失败原因: {item.get('reason', '')}"
            )
            if item.get("prompt"):
                line += f"; 提示词: {item.get('prompt')}"
            lines.append(line)

        lines.append("</comfyui_draw_status>")
        return "<!--EPHEMERAL-->\n" + "\n".join(lines) + "\n<!--/EPHEMERAL-->"

    def inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """把失败状态注入本轮 LLM 请求。

        Args:
            event: 消息事件。
            req: 本轮 LLM 请求。
        """
        status_text = self.build_context(event)
        if not status_text:
            return

        if hasattr(req, "add_user_text"):
            try:
                req.add_user_text(
                    status_text,
                    slot="comfyui_draw_status",
                    after="system_reminder",
                )
            except Exception:
                req.add_user_text(status_text, slot="comfyui_draw_status")
        else:
            current_prompt = getattr(req, "system_prompt", "") or ""
            req.system_prompt = f"{current_prompt}\n\n{status_text}".strip()

        logger.info("[ComfyUI] 已向本轮 LLM 请求注入最近绘图失败状态")


class Drawer:
    """把一次出图请求变成落盘图片。"""

    def __init__(
        self,
        api: Any,
        access: AccessController,
        output_dir: Path,
    ) -> None:
        """记录出图依赖。

        Args:
            api: ComfyUI 客户端，未初始化时可为 None。
            access: 访问控制器。
            output_dir: 图片落盘目录。
        """
        self.api = api
        self.access = access
        self.output_dir = output_dir
        self.failures = FailureLog()

    def ensure_allowed(
        self,
        event: AstrMessageEvent,
        prompts: list[str],
        source: str,
    ) -> None:
        """按准入、敏感词、冷却的顺序检查，失败时记录并抛错。

        Args:
            event: 消息事件。
            prompts: 本次要出的全部提示词。
            source: 失败来源。

        Raises:
            DrawError: 任一检查未通过。
        """
        if not self.api:
            raise self._reject(
                event,
                "❌ ComfyUI 服务未连接，请检查配置",
                prompts,
                source,
            )

        allowed, reason = self.access.check_access(event)
        if not allowed:
            raise self._reject(event, reason, prompts, source)

        for prompt in prompts:
            passed, sensitive = self.access.check_sensitive(prompt, event)
            if passed:
                continue
            tip = "、".join(sensitive[:5])
            extra = f"等 {len(sensitive)} 个" if len(sensitive) > 5 else ""
            message = f"🚫 检测到敏感词：{tip}{extra}，无法生成图片"
            logger.warning(f"[ComfyUI] 用户 {event.get_sender_id()} 触发敏感词: {tip}")
            raise self._reject(event, message, prompt, source)

        ok, remain = self.access.check_cooldown(event)
        if not ok:
            message = f"⚠️ 冷却中，请在 {remain} 秒后重试"
            logger.info(f"[ComfyUI] 用户 {event.get_sender_id()} 冷却中")
            raise self._reject(event, message, prompts, source)

    async def render(
        self,
        event: AstrMessageEvent,
        prompt: str,
        lora_selections: list[dict[str, Any]] | None,
        source: str,
        index: int | None = None,
        append: bool = False,
    ) -> Path:
        """生成一张图并落盘，失败时记录并抛错。

        Args:
            event: 消息事件。
            prompt: 正面提示词。
            lora_selections: LoRA 选择。
            source: 失败来源。
            index: 多图模式下的序号。
            append: 失败记录是否追加。

        Returns:
            落盘后的图片路径。

        Raises:
            DrawError: 生成或保存失败。
        """
        logger.info(
            f"[ComfyUI] 开始生成 | 用户: {event.get_sender_id()} | "
            f"Prompt: {prompt[:50]}..."
        )
        image, error = await self.api.generate(prompt, lora_selections=lora_selections)

        if not image:
            prefix = f"[图片{index}] " if index is not None else "图片"
            message = f"❌ {prefix}生成失败：{error}"
            logger.error(f"[ComfyUI] {prefix}生成失败: {error}")
            self.failures.remember(event, message, prompt, source, append=append)
            raise DrawError(message)

        path = self.output_dir / f"{uuid.uuid4()}.png"
        try:
            await asyncio.to_thread(path.write_bytes, image)
        except OSError as exc:
            message = f"❌ 图片保存失败：{exc}"
            logger.error(f"[ComfyUI] 图片保存失败: {exc}")
            self.failures.remember(event, message, prompt, source, append=append)
            raise DrawError(message) from exc

        logger.info(f"[ComfyUI] 图片已保存: {path.name}")
        return path

    async def draw(
        self,
        event: AstrMessageEvent,
        prompt: str,
        lora_selections: list[dict[str, Any]] | None,
        source: str,
    ) -> Path:
        """完整走一次出图：检查、生成、成功清空失败记录。

        Args:
            event: 消息事件。
            prompt: 正面提示词。
            lora_selections: LoRA 选择。
            source: 失败来源。

        Returns:
            落盘后的图片路径。

        Raises:
            DrawError: 检查、生成或保存失败。
        """
        self.ensure_allowed(event, [prompt], source)
        path = await self.render(event, prompt, lora_selections, source)
        self.failures.clear(event)
        return path

    def _reject(
        self,
        event: AstrMessageEvent,
        message: str,
        prompt: str | list[str],
        source: str,
    ) -> DrawError:
        self.failures.remember(event, message, prompt, source)
        return DrawError(message)
