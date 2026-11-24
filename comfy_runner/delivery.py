"""结果发送：唯一调用 event.send 的地方，含瞬态错误重试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Image, Plain, Record, Video

FILE_OUTPUT_TYPES = {
    "image": Image,
    "video": Video,
    "audio": Record,
}
SEND_MAX_ATTEMPTS = 3
SEND_RETRY_DELAY_SECONDS = 1.0


def artifact_components(
    path: Path,
    output_type: str,
    filename: str,
) -> list[Any]:
    """把一个落盘产物包装成消息组件。

    Args:
        path: 产物路径。
        output_type: 契约声明的产物类型。
        filename: 产物的原始文件名。

    Returns:
        只含一个组件的列表。

    Raises:
        ValueError: 产物类型不是文件类产物。
    """
    component = FILE_OUTPUT_TYPES.get(output_type)
    if component is not None:
        return [component.fromFileSystem(str(path))]
    if output_type == "file":
        return [File(name=filename, file=str(path))]
    raise ValueError(f"产物类型 {output_type} 不是文件类产物")


async def send_chain(event: AstrMessageEvent, components: list[Any]) -> None:
    """主动发送一条组件链，失败时按配置次数重试。

    Args:
        event: 消息事件。
        components: 消息组件。
    """
    last_error: Exception | None = None
    for attempt in range(1, SEND_MAX_ATTEMPTS + 1):
        try:
            await event.send(event.chain_result(components))
            return
        except Exception as exc:
            last_error = exc
            if attempt >= SEND_MAX_ATTEMPTS:
                raise
            logger.warning(
                f"[ComfyUI] 发送消息失败（第 {attempt}/{SEND_MAX_ATTEMPTS} 次），"
                f"即将重试: {exc}"
            )
            await asyncio.sleep(SEND_RETRY_DELAY_SECONDS)
    if last_error is not None:
        raise last_error


async def send_text(event: AstrMessageEvent, text: str) -> None:
    """主动发送一段文本。

    Args:
        event: 消息事件。
        text: 要发送的文本。
    """
    await send_chain(event, [Plain(text=text)])
