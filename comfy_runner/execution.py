"""工作流的一次执行：映射校验、提交、等待与产物解析。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .comfy import (
    ArtifactRef,
    ComfyClient,
    OutputError,
    artifact_ref,
    ensure_success,
)
from .contract import (
    ContractError,
    ToolContract,
    input_target_errors,
    read_workflow,
)


@dataclass(frozen=True, slots=True)
class Artifact:
    """一个下载完成的正式产物。"""

    output: str
    ref: ArtifactRef
    content: bytes

    @property
    def suffix(self) -> str:
        """产物文件的扩展名。"""
        return Path(self.ref.filename).suffix or ".png"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """一次执行的产物集合。"""

    prompt_id: str
    artifacts: tuple[Artifact, ...]


class ConcurrencyGate:
    """入口并发名额；超限直接拒绝，不建等待队列。"""

    def __init__(self, limit: int) -> None:
        """设置并发上限。

        Args:
            limit: 允许同时执行的最大数量。

        Raises:
            ValueError: 上限小于 1。
        """
        if limit < 1:
            raise ValueError(f"并发上限必须大于 0，实际为 {limit}")
        self._limit = limit
        self._active = 0

    @property
    def limit(self) -> int:
        """并发上限。"""
        return self._limit

    @property
    def active(self) -> int:
        """当前正在执行的数量。"""
        return self._active

    def try_acquire(self) -> bool:
        """尝试占用一个名额。

        Returns:
            成功占用时为 True；已满时为 False。
        """
        if self._active >= self._limit:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        """释放一个名额。

        Raises:
            RuntimeError: 没有可释放的名额。
        """
        if self._active <= 0:
            raise RuntimeError("并发名额已经全部释放")
        self._active -= 1


class WorkflowExecutor:
    """按契约提交并等待一次工作流执行。"""

    def __init__(self, client: ComfyClient) -> None:
        """记录 ComfyUI 客户端。

        Args:
            client: 提交与下载使用的客户端。
        """
        self.client = client

    async def submit(
        self,
        contract: ToolContract,
        arguments: dict[str, Any],
    ) -> str:
        """校验映射并提交一次执行。

        Args:
            contract: 工具契约。
            arguments: 本次调用显式提供的参数。

        Returns:
            ComfyUI 分配的任务 ID。

        Raises:
            ContractError: 工作流文件里契约声明的映射已不成立。
            ComfyError: 提交失败或提交结果未知。
        """
        graph = read_workflow(contract.workflow_path)
        problems = input_target_errors(contract, graph, contract.path.name)
        if problems:
            raise ContractError("\n".join(problems))

        apply_arguments(contract, graph, arguments)
        logger.info(f"[ComfyUI] 提交工作流 {contract.name}")

        prompt_id = await self.client.submit(graph)
        logger.info(f"[ComfyUI] 已接受 {contract.name} | prompt_id: {prompt_id}")
        return prompt_id

    async def collect(
        self,
        contract: ToolContract,
        prompt_id: str,
    ) -> ExecutionResult:
        """等待同一个任务完成并下载声明的正式产物。

        Args:
            contract: 工具契约。
            prompt_id: 已经提交的任务 ID。

        Returns:
            任务 ID 与全部正式产物。

        Raises:
            ComfyError: 执行失败、状态未知或产物下载失败。
        """
        entry = await self.client.wait(prompt_id)

        artifacts: list[Artifact] = []
        for output, ref in declared_refs(contract, entry):
            content = await self.client.download(ref)
            artifacts.append(Artifact(output=output, ref=ref, content=content))

        logger.info(
            f"[ComfyUI] 执行完成 {contract.name} | prompt_id: {prompt_id} | "
            f"产物: {len(artifacts)} 个"
        )
        return ExecutionResult(prompt_id=prompt_id, artifacts=tuple(artifacts))


def apply_arguments(
    contract: ToolContract,
    graph: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    """只覆盖本次显式提供的参数。

    Args:
        contract: 工具契约。
        graph: 本次执行专用的工作流副本。
        arguments: 本次调用显式提供的参数。
    """
    for name, value in arguments.items():
        parameter = contract.inputs[name]
        graph[parameter.node]["inputs"][parameter.field] = value
        logger.debug(f"[ComfyUI] 覆盖 {parameter.target} <- {name}")


def declared_refs(
    contract: ToolContract,
    entry: dict[str, Any],
) -> list[tuple[str, ArtifactRef]]:
    """从执行结果里取出全部正式产物位置。

    Args:
        contract: 工具契约。
        entry: ``history`` 里的任务结果。

    Returns:
        产物名与下载位置，同一字段内保持 ComfyUI 返回顺序，
        不同输出映射按产物名排序。

    Raises:
        ExecutionFailed: 执行状态为失败。
        ExecutionUnknown: 执行状态不完整。
        OutputError: 声明的产物缺失或类型不符。
    """
    ensure_success(entry)

    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        raise OutputError("执行结果缺少 outputs")

    refs: list[tuple[str, ArtifactRef]] = []
    for name in sorted(contract.outputs):
        mapping = contract.outputs[name]
        node_output = outputs.get(mapping.node)
        if not isinstance(node_output, dict):
            raise OutputError(f"产物 {name} 的输出节点 {mapping.node} 没有结果")

        items = node_output.get(mapping.field)
        if not isinstance(items, list) or not items:
            raise OutputError(f"产物 {name} 的字段 {mapping.target} 没有内容")

        for index, item in enumerate(items, start=1):
            ref = artifact_ref(item)
            if ref is None:
                raise OutputError(f"产物 {name} 的第 {index} 项不是可下载的产物")
            refs.append((name, ref))
    return refs
