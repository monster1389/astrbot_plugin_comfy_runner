"""结果发送：唯一调用 event.send / event.chain_result 的地方。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Image, Node


def text_result(event: AstrMessageEvent, text: str) -> MessageEventResult:
    """构造一条纯文本回复。

    Args:
        event: 消息事件。
        text: 回复文本。

    Returns:
        可直接 yield 或 send 的结果。
    """
    return event.plain_result(text)


def chain_result(event: AstrMessageEvent, components: list[Any]) -> MessageEventResult:
    """构造一条组件链回复。

    Args:
        event: 消息事件。
        components: 消息组件。

    Returns:
        可直接 yield 或 send 的结果。
    """
    return event.chain_result(components)


def image_components(path: Path) -> list[Any]:
    """把本地图片文件包装成消息组件。

    Args:
        path: 图片路径。

    Returns:
        只含一张图片的组件列表。
    """
    return [Image.fromFileSystem(str(path))]


def forward_components(path: Path, sender_id: str) -> list[Any]:
    """把本地图片包装成合并转发节点。

    Args:
        path: 图片路径。
        sender_id: 转发节点显示的账号。

    Returns:
        只含一个转发节点的组件列表。
    """
    return [
        Node(
            user_id=int(sender_id),
            nickname="ComfyUI",
            content=[Image.fromFileSystem(str(path))],
        )
    ]


async def send_text(event: AstrMessageEvent, text: str) -> None:
    """主动发送一条纯文本。

    Args:
        event: 消息事件。
        text: 回复文本。
    """
    await event.send(event.plain_result(text))


async def send_chain(event: AstrMessageEvent, components: list[Any]) -> None:
    """主动发送一条组件链。

    Args:
        event: 消息事件。
        components: 消息组件。
    """
    await event.send(event.chain_result(components))
