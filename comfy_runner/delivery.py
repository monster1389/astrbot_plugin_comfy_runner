"""结果发送：唯一调用 event.send 的地方。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image


def image_components(path: Path) -> list[Any]:
    """把本地图片文件包装成消息组件。

    Args:
        path: 图片路径。

    Returns:
        只含一张图片的组件列表。
    """
    return [Image.fromFileSystem(str(path))]


async def send_chain(event: AstrMessageEvent, components: list[Any]) -> None:
    """主动发送一条组件链。

    Args:
        event: 消息事件。
        components: 消息组件。
    """
    await event.send(event.chain_result(components))
