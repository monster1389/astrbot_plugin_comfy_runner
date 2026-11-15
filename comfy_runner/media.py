"""附件与参数引用：把模型给出的引用变成一份可上传的文件。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import (
    File,
    Image,
    Record,
    Reply,
    Video,
    file_uri_to_path,
    is_file_uri,
)

KIND_COMPONENTS = {
    "image": Image,
    "video": Video,
    "audio": Record,
    "file": File,
}
FILE_KIND_EXTENSIONS = {
    "image": frozenset(
        {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
    ),
    "video": frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}),
    "audio": frozenset(
        {".mp3", ".wav", ".ogg", ".opus", ".flac", ".m4a", ".aac", ".amr"}
    ),
}


def _file_extension(component: File) -> str:
    for candidate in (
        getattr(component, "name", ""),
        getattr(component, "url", ""),
        getattr(component, "file_", ""),
    ):
        if not candidate:
            continue
        suffix = Path(urlparse(str(candidate)).path).suffix.lower()
        if suffix:
            return suffix
    return ""


def _matches_kind(component: Any, kind: str, component_type: type[Any]) -> bool:
    if isinstance(component, component_type):
        return True
    if not isinstance(component, File):
        return False
    extensions = FILE_KIND_EXTENSIONS.get(kind)
    return bool(extensions and _file_extension(component) in extensions)
ATTACHMENT_PREFIX = "attachment"
DOWNLOAD_TIMEOUT_SECONDS = 60.0


class MediaError(ValueError):
    """参数引用无法解析成一份可上传的文件。"""


@dataclass(frozen=True, slots=True)
class MediaPayload:
    """一份待上传到 ComfyUI 的文件。"""

    content: bytes
    suffix: str

    @property
    def filename(self) -> str:
        """按内容哈希生成的上传文件名，同样内容只落一份。"""
        digest = hashlib.sha256(self.content).hexdigest()[:16]
        return f"astrbot_{digest}{self.suffix}"


@dataclass(frozen=True, slots=True)
class Attachments:
    """本条消息与引用消息里的附件，按类型与出现顺序排列。"""

    items: dict[str, tuple[Any, ...]]

    @classmethod
    def from_event(cls, event: AstrMessageEvent) -> Attachments:
        """按"本条消息优先、引用消息在后"的顺序收集附件。

        Args:
            event: 触发本次调用的消息事件。

        Returns:
            文件类参数可以引用的附件集合。
        """
        ordered: list[Any] = list(event.get_messages())
        for component in list(ordered):
            if isinstance(component, Reply) and component.chain:
                ordered.extend(component.chain)
        return cls(
            items={
                kind: tuple(
                    component
                    for component in ordered
                    if _matches_kind(component, kind, component_type)
                )
                for kind, component_type in KIND_COMPONENTS.items()
            }
        )

    def pick(self, kind: str, index: int) -> Any:
        """取出某个类型的第 index 个附件。

        Args:
            kind: 参数类型。
            index: 从 1 开始的序号。

        Returns:
            对应的消息组件。

        Raises:
            MediaError: 序号超出范围。
        """
        components = self.items.get(kind, ())
        label = KIND_LABELS[kind]
        if index < 1 or index > len(components):
            raise MediaError(
                f"消息里没有第 {index} 个{label}附件（共有 {len(components)} 个）"
            )
        return components[index - 1]


KIND_LABELS = {
    "image": "图片",
    "video": "视频",
    "audio": "语音",
    "file": "文件",
}


async def resolve_reference(
    value: str,
    kind: str,
    attachments: Attachments,
) -> MediaPayload:
    """把模型给出的引用解析成一份待上传的文件。

    Args:
        value: 参数值，形如 ``attachment``、``attachment:2``、``file://`` URI、
            http(s) 链接或本地绝对路径。
        kind: 参数类型，决定 ``attachment`` 取哪一类附件。
        attachments: 本次消息里的附件集合。

    Returns:
        文件内容与扩展名。

    Raises:
        MediaError: 引用格式无法识别或内容取不到。
    """
    text = value.strip()
    index = _attachment_index(text)
    if index is not None:
        component = attachments.pick(kind, index)
        path = Path(await _component_path(component))
        return MediaPayload(
            content=await _read_bytes(path, value),
            suffix=_require_suffix(path.suffix, value),
        )

    if is_file_uri(text):
        path = Path(file_uri_to_path(text))
        return MediaPayload(
            content=await _read_bytes(path, value),
            suffix=_require_suffix(path.suffix, value),
        )

    if text.startswith(("http://", "https://")):
        suffix = _require_suffix(Path(urlparse(text).path).suffix, value)
        return MediaPayload(content=await _download(text), suffix=suffix)

    path = Path(text)
    if path.is_absolute() and path.is_file():
        return MediaPayload(
            content=await _read_bytes(path, value),
            suffix=_require_suffix(path.suffix, value),
        )

    raise MediaError(
        f"无法解析的引用 {value!r}：请填 attachment、attachment:序号、"
        "file:// URI、http(s) 链接或本地绝对路径"
    )


def _attachment_index(text: str) -> int | None:
    if text == ATTACHMENT_PREFIX:
        return 1
    prefix = f"{ATTACHMENT_PREFIX}:"
    if not text.startswith(prefix):
        return None
    tail = text[len(prefix) :]
    if not tail.isdigit():
        raise MediaError(f"无法解析的附件序号 {tail!r}：应写成 attachment:2")
    return int(tail)


def _require_suffix(suffix: str, value: str) -> str:
    if not suffix:
        raise MediaError(f"无法判断 {value!r} 的文件类型：引用需要带扩展名")
    return suffix


async def _component_path(component: Any) -> str:
    try:
        path = (
            await component.get_file()
            if isinstance(component, File)
            else await component.convert_to_file_path()
        )
    except (OSError, aiohttp.ClientError) as exc:
        raise MediaError(f"读取消息附件失败: {exc}") from exc
    if not path:
        raise MediaError("读取消息附件失败: 未获取到有效文件路径")
    return path


async def _read_bytes(path: Path, value: str) -> bytes:
    try:
        return await asyncio.to_thread(path.read_bytes)
    except OSError as exc:
        raise MediaError(f"读取 {value!r} 失败: {exc}") from exc


async def _download(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise MediaError(f"下载 {url} 失败: HTTP {resp.status}")
                return await resp.read()
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise MediaError(f"下载 {url} 失败: {exc}") from exc
