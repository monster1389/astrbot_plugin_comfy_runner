"""把工作流契约注册为 AstrBot Function Tool。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent

from .contract import ToolContract, validate_arguments
from .registry import WorkflowTool

ToolHandler = Callable[..., Awaitable[str]]


def build_function_tools(
    tools: list[WorkflowTool],
    module_path: str,
) -> list[FunctionTool]:
    """把工作流工具转换成 Function Tool。

    Args:
        tools: 已通过校验的工作流工具。
        module_path: 注册方模块路径，AstrBot 卸载插件时按它回收工具。

    Returns:
        可直接交给 ``Context.add_llm_tools`` 的工具列表。
    """
    built: list[FunctionTool] = []
    for tool in tools:
        function_tool = FunctionTool(
            name=tool.contract.name,
            description=tool.contract.description,
            parameters=tool.parameters,
            handler=make_handler(tool.contract),
        )
        function_tool.__module__ = module_path
        built.append(function_tool)
    return built


def make_handler(contract: ToolContract) -> ToolHandler:
    """为一个契约生成工具入口。

    Args:
        contract: 工具契约。

    Returns:
        接收 ``event`` 与模型参数的工具处理函数。
    """

    async def handler(event: AstrMessageEvent, **arguments: Any) -> str:
        validate_arguments(contract, arguments)
        raise RuntimeError("工作流执行尚未接入")

    return handler
