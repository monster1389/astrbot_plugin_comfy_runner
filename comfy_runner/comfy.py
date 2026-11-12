"""ComfyUI 的 HTTP 接口：提交工作流、等待完成、下载图片。"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any

import aiohttp

DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_POLL_ATTEMPTS = 300


class ComfyError(RuntimeError):
    """与 ComfyUI 交互失败。"""


class ComfyOutputError(ComfyError):
    """工作流执行完成，但取不到输出图片。"""


def normalize_address(address: str) -> str:
    """补全 ComfyUI 地址的协议头并去掉结尾斜杠。

    Args:
        address: 配置里的服务地址，可带或不带协议头。

    Returns:
        可以直接拼接路径的地址。
    """
    address = (address or "").strip()
    if not address:
        return address
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", address):
        address = f"http://{address}"
    return address.rstrip("/")


class ComfyClient:
    """对 ComfyUI REST 接口的最小封装。"""

    def __init__(
        self,
        address: str,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    ) -> None:
        """初始化客户端。

        Args:
            address: ComfyUI 服务地址。
            poll_interval: 轮询间隔，单位秒。
            poll_attempts: 轮询次数上限。
        """
        self.base_url = normalize_address(address)
        self.poll_interval = poll_interval
        self.poll_attempts = poll_attempts

    async def generate(self, graph: dict[str, Any], output_node_id: str) -> bytes:
        """提交工作流并等待出图。

        Args:
            graph: 完整的 ComfyUI 工作流图。
            output_node_id: 输出节点 ID，留空时自动发现。

        Returns:
            图片字节。

        Raises:
            ComfyError: 提交、查询或下载失败。
            ComfyOutputError: 工作流完成但没有图片。
        """
        async with aiohttp.ClientSession() as session:
            prompt_id = await self._submit(session, graph)
            image = await self._wait_for_image(session, prompt_id, output_node_id)
            return await self._download(session, image)

    async def _submit(
        self,
        session: aiohttp.ClientSession,
        graph: dict[str, Any],
    ) -> str:
        payload = {
            "prompt": graph,
            "client_id": str(random.randint(100000, 999999)),
        }
        try:
            async with session.post(f"{self.base_url}/prompt", json=payload) as resp:
                if resp.status != 200:
                    raise ComfyError(f"连接 ComfyUI 失败: {resp.status}")
                data = await resp.json()
        except aiohttp.ClientError as exc:
            raise ComfyError(f"请求报错: {exc}") from exc

        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyError("ComfyUI 未返回 prompt_id")
        return str(prompt_id)

    async def _wait_for_image(
        self,
        session: aiohttp.ClientSession,
        prompt_id: str,
        output_node_id: str,
    ) -> dict[str, Any]:
        for _ in range(self.poll_attempts):
            await asyncio.sleep(self.poll_interval)
            image = await self._poll(session, prompt_id, output_node_id)
            if image is not None:
                return image
        raise ComfyError("生成超时")

    async def _poll(
        self,
        session: aiohttp.ClientSession,
        prompt_id: str,
        output_node_id: str,
    ) -> dict[str, Any] | None:
        try:
            async with session.get(f"{self.base_url}/history/{prompt_id}") as resp:
                if resp.status != 200:
                    raise ComfyError(f"查询任务失败: {resp.status}")
                history = await resp.json()
        except aiohttp.ClientError as exc:
            raise ComfyError(f"与 ComfyUI 的连接中断: {exc}") from exc

        if prompt_id not in history:
            return None
        return self._select_image(history[prompt_id].get("outputs", {}), output_node_id)

    def _select_image(
        self,
        outputs: dict[str, Any],
        output_node_id: str,
    ) -> dict[str, Any]:
        if output_node_id:
            images = outputs.get(output_node_id, {}).get("images", [])
            if not images:
                raise ComfyOutputError(f"输出节点 {output_node_id} 没有图片")
            return images[0]

        for node_output in outputs.values():
            images = node_output.get("images", [])
            if images:
                return images[0]
        raise ComfyOutputError("工作流执行完成，但未找到输出图片")

    async def _download(
        self,
        session: aiohttp.ClientSession,
        image: dict[str, Any],
    ) -> bytes:
        url = (
            f"{self.base_url}/view?filename={image['filename']}"
            f"&subfolder={image['subfolder']}&type={image['type']}"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise ComfyError("下载图片失败")
                return await resp.read()
        except aiohttp.ClientError as exc:
            raise ComfyError(f"下载图片失败: {exc}") from exc
