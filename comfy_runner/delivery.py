"""结果发送：唯一调用 event.send 的地方。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Image, Plain, Record, Video

FILE_OUTPUT_TYPES = {
    "image": Image,
    "video": Video,
    "audio": Record,
}


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
    """主动发送一条组件链。

    Args:
        event: 消息事件。
        components: 消息组件。
    """
    await event.send(event.chain_result(components))


async def send_text(event: AstrMessageEvent, text: str) -> None:
    """主动发送一段文本。

    Args:
        event: 消息事件。
        text: 要发送的文本。
    """
    await send_chain(event, [Plain(text=text)])
