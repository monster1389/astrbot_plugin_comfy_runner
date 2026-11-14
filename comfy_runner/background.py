"""插件内的后台任务：启动、回收与卸载时取消。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from astrbot.api import logger


class BackgroundJobs:
    """持有本插件启动的后台任务。"""

    def __init__(self) -> None:
        """初始化空的任务集合。"""
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def tasks(self) -> tuple[asyncio.Task[None], ...]:
        """当前尚未结束的后台任务。"""
        return tuple(self._tasks)

    def start(
        self,
        coroutine: Coroutine[Any, Any, None],
        label: str,
    ) -> asyncio.Task[None]:
        """启动一个后台任务。

        Args:
            coroutine: 后台协程。
            label: 出现在日志与任务名里的标识。

        Returns:
            新建的任务，可用于登记结束回调。
        """
        task = asyncio.create_task(coroutine, name=label)
        self._tasks.add(task)
        task.add_done_callback(self._finish)
        logger.info(f"[ComfyUI] 后台任务已启动: {label}")
        return task

    async def cancel_all(self) -> None:
        """取消并等待全部后台任务结束。"""
        tasks = tuple(self._tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"[ComfyUI] 已取消 {len(tasks)} 个后台任务")

    def _finish(self, task: asyncio.Task[None]) -> None:
        """回收已结束的任务并记录未被处理的异常。

        Args:
            task: 刚结束的后台任务。
        """
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        logger.error(
            f"[ComfyUI] 后台任务 {task.get_name()} 失败: {error}",
            exc_info=error,
        )
