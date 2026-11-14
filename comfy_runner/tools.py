"""工作流工具入口：准入、并发、执行与产物交付。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent

from .access import AccessController
from .contract import ToolContract, validate_arguments
from .delivery import image_components, send_chain
from .execution import ConcurrencyGate, ExecutionResult, WorkflowExecutor
from .registry import WorkflowTool

ToolHandler = Callable[..., Awaitable[str]]


class WorkflowRejected(RuntimeError):
    """入口拒绝本次调用，消息面向用户。"""


class WorkflowToolAdapter:
    """把工具调用接到访问控制、执行与交付上。"""

    def __init__(
        self,
        access: AccessController,
        executor: WorkflowExecutor,
        gate: ConcurrencyGate,
        output_dir: Path,
    ) -> None:
        """记录入口需要的依赖。

        Args:
            access: 访问控制器。
            executor: 工作流执行器。
            gate: 入口并发名额。
            output_dir: 产物落盘目录。
        """
        self.access = access
        self.executor = executor
        self.gate = gate
        self.output_dir = output_dir

    async def call(
        self,
        contract: ToolContract,
        event: AstrMessageEvent,
        arguments: dict[str, Any],
    ) -> str:
        """执行一次工具调用并把产物发到当前会话。

        Args:
            contract: 工具契约。
            event: 触发本次调用的消息事件。
            arguments: 模型给出的参数。

        Returns:
            给模型的简短结果。

        Raises:
            ContractError: 参数不合法。
            WorkflowRejected: 准入、敏感词、并发或冷却未通过。
            ComfyError: 提交、执行、产物或下载失败。
        """
        validate_arguments(contract, arguments)
        await self._admit(contract, event, arguments)

        try:
            result = await self.executor.execute(contract, arguments)
        finally:
            self.gate.release()

        paths = await self._store(result)
        await self._deliver(event, paths)
        return f"已生成 {len(paths)} 个产物并发送到当前会话"

    async def _admit(
        self,
        contract: ToolContract,
        event: AstrMessageEvent,
        arguments: dict[str, Any],
    ) -> None:
        """按准入、敏感词、并发、冷却的顺序检查。"""
        allowed, reason = self.access.check_access(event)
        if not allowed:
            raise WorkflowRejected(reason)

        for text in _free_texts(contract, arguments):
            passed, words = self.access.check_sensitive(text, event)
            if passed:
                continue
            shown = "、".join(words[:5])
            more = f" 等 {len(words)} 个" if len(words) > 5 else ""
            logger.warning(
                f"[ComfyUI] 用户 {event.get_sender_id()} 触发敏感词: {shown}"
            )
            raise WorkflowRejected(
                f"🚫 检测到敏感词：{shown}{more}，无法执行 {contract.name}"
            )

        if not self.gate.try_acquire():
            logger.warning(f"[ComfyUI] {contract.name} 并发已满，拒绝本次调用")
            raise WorkflowRejected(f"⚠️ 并发已满（上限 {self.gate.limit}），请稍后重试")

        passed, remain = self.access.check_cooldown(event)
        if not passed:
            self.gate.release()
            logger.info(f"[ComfyUI] 用户 {event.get_sender_id()} 冷却中")
            raise WorkflowRejected(f"⚠️ 冷却中，请在 {remain} 秒后重试")

    async def _store(self, result: ExecutionResult) -> list[Path]:
        """把全部正式产物写到落盘目录。"""
        paths: list[Path] = []
        for artifact in result.artifacts:
            path = self.output_dir / f"{uuid.uuid4().hex}{artifact.suffix}"
            await asyncio.to_thread(path.write_bytes, artifact.content)
            paths.append(path)
        return paths

    async def _deliver(self, event: AstrMessageEvent, paths: list[Path]) -> None:
        """把全部正式产物作为一条消息链发到当前会话。"""
        components = [
            component for path in paths for component in image_components(path)
        ]
        await send_chain(event, components)
        logger.info(f"[ComfyUI] 已发送 {len(paths)} 个产物到当前会话")


def build_function_tools(
    tools: list[WorkflowTool],
    module_path: str,
    adapter: WorkflowToolAdapter,
) -> list[FunctionTool]:
    """把工作流工具转换成 Function Tool。

    Args:
        tools: 已通过校验的工作流工具。
        module_path: 注册方模块路径，AstrBot 卸载插件时按它回收工具。
        adapter: 工具调用的入口。

    Returns:
        可直接交给 ``Context.add_llm_tools`` 的工具列表。
    """
    built: list[FunctionTool] = []
    for tool in tools:
        function_tool = FunctionTool(
            name=tool.contract.name,
            description=tool.contract.description,
            parameters=tool.parameters,
            handler=make_handler(tool.contract, adapter),
        )
        function_tool.__module__ = module_path
        built.append(function_tool)
    return built


def make_handler(contract: ToolContract, adapter: WorkflowToolAdapter) -> ToolHandler:
    """为一个契约生成工具入口。

    Args:
        contract: 工具契约。
        adapter: 工具调用的入口。

    Returns:
        接收 ``event`` 与模型参数的工具处理函数。
    """

    async def handler(event: AstrMessageEvent, **arguments: Any) -> str:
        return await adapter.call(contract, event, arguments)

    return handler


def _free_texts(contract: ToolContract, arguments: dict[str, Any]) -> list[str]:
    """取出需要检查敏感词的自由文本参数。"""
    texts: list[str] = []
    for name, parameter in contract.inputs.items():
        if parameter.type != "string" or parameter.enum:
            continue
        value = arguments.get(name)
        if isinstance(value, str) and value:
            texts.append(value)
    return texts
