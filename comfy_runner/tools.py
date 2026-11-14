"""把工作流契约注册为 AstrBot Function Tool。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent

from .comfy import ComfyError
from .contract import ToolContract, validate_arguments
from .execution import ConcurrencyGate, WorkflowExecutor
from .registry import WorkflowTool

ToolHandler = Callable[..., Awaitable[str]]


def build_function_tools(
    tools: list[WorkflowTool],
    module_path: str,
    executor: WorkflowExecutor,
    gate: ConcurrencyGate,
) -> list[FunctionTool]:
    """把工作流工具转换成 Function Tool。

    Args:
        tools: 已通过校验的工作流工具。
        module_path: 注册方模块路径，AstrBot 卸载插件时按它回收工具。
        executor: 执行工作流使用的执行器。
        gate: 入口并发名额。

    Returns:
        可直接交给 ``Context.add_llm_tools`` 的工具列表。
    """
    built: list[FunctionTool] = []
    for tool in tools:
        function_tool = FunctionTool(
            name=tool.contract.name,
            description=tool.contract.description,
            parameters=tool.parameters,
            handler=make_handler(tool.contract, executor, gate),
        )
        function_tool.__module__ = module_path
        built.append(function_tool)
    return built


def make_handler(
    contract: ToolContract,
    executor: WorkflowExecutor,
    gate: ConcurrencyGate,
) -> ToolHandler:
    """为一个契约生成工具入口。

    Args:
        contract: 工具契约。
        executor: 执行工作流使用的执行器。
        gate: 入口并发名额。

    Returns:
        接收 ``event`` 与模型参数的工具处理函数。
    """

    async def handler(event: AstrMessageEvent, **arguments: Any) -> str:
        validate_arguments(contract, arguments)

        if not gate.try_acquire():
            logger.warning(f"[ComfyUI] {contract.name} 并发已满，拒绝本次调用")
            raise ComfyError(f"并发已满（上限 {gate.limit}），本次调用被拒绝")

        try:
            result = await executor.execute(contract, arguments)
        finally:
            gate.release()

        return f"已生成 {len(result.artifacts)} 个产物"

    return handler
