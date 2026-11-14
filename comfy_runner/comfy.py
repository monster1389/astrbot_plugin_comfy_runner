"""ComfyUI 的 HTTP 接口：提交工作流、等待完成、下载产物。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_POLL_INTERVAL = 1.0


class ComfyError(RuntimeError):
    """与 ComfyUI 交互失败。"""


class SubmitError(ComfyError):
    """ComfyUI 明确拒绝了这次工作流提交。"""


class ExecutionUnknown(ComfyError):
    """无法确认 ComfyUI 是否接受或完成了这次执行。"""


class ExecutionFailed(ComfyError):
    """ComfyUI 执行了工作流但报告失败。"""


class ExecutionTimeout(ComfyError):
    """在超时上限内没有等到执行结果。"""


class OutputError(ComfyError):
    """执行完成但声明的正式产物不完整。"""


class DownloadError(ComfyError):
    """下载正式产物失败。"""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """一个待下载的产物位置。"""

    filename: str
    subfolder: str
    type: str


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


def artifact_ref(payload: Any) -> ArtifactRef | None:
    """把 history 里的产物条目转成下载位置。

    Args:
        payload: ``outputs`` 里的单个元素。

    Returns:
        产物位置；结构不符时返回 None。
    """
    if not isinstance(payload, dict):
        return None
    filename = payload.get("filename")
    subfolder = payload.get("subfolder")
    kind = payload.get("type")
    if not isinstance(filename, str) or not filename:
        return None
    if not isinstance(subfolder, str):
        return None
    if not isinstance(kind, str) or not kind:
        return None
    return ArtifactRef(filename=filename, subfolder=subfolder, type=kind)


def failure_detail(status: dict[str, Any]) -> str:
    """从执行状态里取出失败原因。

    Args:
        status: ``history`` 条目里的 ``status``。

    Returns:
        面向用户的一句话失败原因。
    """
    messages = status.get("messages")
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, list) or len(message) != 2:
            continue
        if message[0] != "execution_error":
            continue
        data = message[1]
        if not isinstance(data, dict):
            break
        node = data.get("node_id") or data.get("node_type") or ""
        reason = data.get("exception_message") or data.get("exception_type") or ""
        detail = " ".join(part for part in (str(node), str(reason)) if part).strip()
        return f"执行失败: {detail}" if detail else "执行失败"
    return "执行失败"


class ComfyClient:
    """对 ComfyUI REST 接口的最小封装。"""

    def __init__(
        self,
        address: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        """初始化客户端。

        Args:
            address: ComfyUI 服务地址。
            timeout: 单次执行的总超时，单位秒。
            poll_interval: 查询执行状态的间隔，单位秒。

        Raises:
            ValueError: 超时或轮询间隔不是正数。
        """
        if timeout <= 0:
            raise ValueError(f"执行超时必须是正数，实际为 {timeout}")
        if poll_interval <= 0:
            raise ValueError(f"轮询间隔必须是正数，实际为 {poll_interval}")
        self.base_url = normalize_address(address)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self.client_id = f"astrbot-comfy-runner-{uuid.uuid4().hex[:8]}"

    async def object_info(self, class_type: str) -> dict[str, Any]:
        """读取一个节点类型的定义。

        Args:
            class_type: 节点类型名。

        Returns:
            该节点类型的定义对象。

        Raises:
            ComfyError: 请求失败或返回结构不符。
        """
        url = f"{self.base_url}/object_info/{quote(class_type)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=self._request_timeout()) as resp:
                    if resp.status != 200:
                        raise ComfyError(f"读取节点定义失败: HTTP {resp.status}")
                    data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ComfyError(f"读取节点定义失败: {exc}") from exc

        definition = data.get(class_type) if isinstance(data, dict) else None
        if not isinstance(definition, dict):
            raise ComfyError(f"ComfyUI 未返回节点类型 {class_type} 的定义")
        return definition

    async def submit(self, graph: dict[str, Any]) -> str:
        """提交工作流。

        Args:
            graph: 本次要执行的完整工作流图。

        Returns:
            ComfyUI 分配的任务 ID。

        Raises:
            SubmitError: ComfyUI 明确拒绝这次提交。
            ExecutionUnknown: 无法确认 ComfyUI 是否接受这次提交。
        """
        payload = {"prompt": graph, "client_id": self.client_id}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/prompt",
                    json=payload,
                    timeout=self._request_timeout(),
                ) as resp:
                    status = resp.status
                    body = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ExecutionUnknown(f"执行状态未知，提交结果不明: {exc}") from exc

        if status != 200:
            raise SubmitError(f"提交失败: HTTP {status}")

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ExecutionUnknown(f"执行状态未知，返回不是 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ExecutionUnknown("执行状态未知，返回不是对象")

        error = data.get("error")
        node_errors = data.get("node_errors")
        if error or node_errors:
            raise SubmitError(_submit_detail(error, node_errors))

        prompt_id = data.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise SubmitError("ComfyUI 未返回 prompt_id")
        return prompt_id

    async def history(self, prompt_id: str) -> dict[str, Any] | None:
        """查询同一个任务的执行结果。

        Args:
            prompt_id: 任务 ID。

        Returns:
            任务结果；任务尚未出现时为 None。

        Raises:
            ComfyError: 查询失败或返回结构不符。
        """
        url = f"{self.base_url}/history/{quote(prompt_id)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=self._request_timeout()) as resp:
                    if resp.status != 200:
                        raise ComfyError(f"查询任务失败: HTTP {resp.status}")
                    data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ComfyError(f"查询任务失败: {exc}") from exc

        if not isinstance(data, dict):
            raise ComfyError("查询任务返回的不是对象")
        entry = data.get(prompt_id)
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise ComfyError(f"任务 {prompt_id} 的结果结构不符")
        return entry

    async def wait(self, prompt_id: str) -> dict[str, Any]:
        """轮询同一个任务直到出现结果。

        超过总超时后抛错，不重新提交。

        Args:
            prompt_id: 任务 ID。

        Returns:
            任务结果。

        Raises:
            ExecutionTimeout: 超时仍未出现结果。
            ExecutionUnknown: 查询失败，无法确认执行状态。
        """
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                entry = await self.history(prompt_id)
            except ComfyError as exc:
                raise ExecutionUnknown(f"执行状态未知: {exc}") from exc
            if entry is not None:
                return entry
            if time.monotonic() >= deadline:
                raise ExecutionTimeout(f"生成超时（{self.timeout:g} 秒）")
            await asyncio.sleep(self.poll_interval)

    async def download(self, ref: ArtifactRef) -> bytes:
        """下载一个正式产物。

        Args:
            ref: 产物位置。

        Returns:
            产物字节。

        Raises:
            DownloadError: 请求失败或返回非 200。
        """
        query = urlencode(
            {
                "filename": ref.filename,
                "subfolder": ref.subfolder,
                "type": ref.type,
            }
        )
        url = f"{self.base_url}/view?{query}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=self._request_timeout()) as resp:
                    if resp.status != 200:
                        raise DownloadError(
                            f"下载 {ref.filename} 失败: HTTP {resp.status}"
                        )
                    return await resp.read()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise DownloadError(f"下载 {ref.filename} 失败: {exc}") from exc

    def _request_timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=self.timeout)


def _submit_detail(error: Any, node_errors: Any) -> str:
    parts: list[str] = []
    if isinstance(error, dict):
        kind = str(error.get("type") or "")
        message = str(error.get("message") or "")
        detail = str(error.get("details") or "")
        text = " ".join(part for part in (kind, message, detail) if part).strip()
        if text:
            parts.append(text)
    elif error:
        parts.append(str(error))

    if isinstance(node_errors, dict):
        for node_id in sorted(node_errors):
            entry = node_errors[node_id]
            if not isinstance(entry, dict):
                continue
            for item in entry.get("errors") or []:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("type") or "")
                message = str(item.get("message") or "")
                text = " ".join(part for part in (kind, message) if part).strip()
                if text:
                    parts.append(f"节点 {node_id}: {text}")

    return "提交失败: " + "; ".join(parts) if parts else "提交失败"


def ensure_success(entry: dict[str, Any]) -> None:
    """确认任务结果表示执行成功。

    Args:
        entry: ``history`` 里的任务结果。

    Raises:
        ExecutionFailed: 状态为 error。
        ExecutionUnknown: 状态缺失或未完成。
    """
    status = entry.get("status")
    if not isinstance(status, dict):
        raise ExecutionUnknown("执行结果缺少 status")
    if status.get("status_str") == "error":
        raise ExecutionFailed(failure_detail(status))
    if not status.get("completed"):
        raise ExecutionUnknown("执行未完成，无法确认结果")
