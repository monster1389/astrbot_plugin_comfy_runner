"""工作流工具入口：清单、契约查看、提交执行与后台交付。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent

from .access import AccessController
from .background import BackgroundJobs
from .comfy import ComfyError, ExecutionTimeout, ExecutionUnknown
from .contract import (
    ContractError,
    ToolContract,
    contract_view,
    input_target_errors,
    read_workflow,
    validate_arguments,
)
from .delivery import artifact_components, send_chain, send_text
from .execution import (
    ConcurrencyGate,
    ExecutionResult,
    FileArtifact,
    TextArtifact,
    WorkflowExecutor,
)
from .media import Attachments, MediaError

ToolHandler = Callable[..., Awaitable[str]]

LIST_NAME = "comfy_list"
INSPECT_NAME = "comfy_inspect"
EXECUTE_NAME = "comfy_execute"

LIST_DESCRIPTION = (
    "列出可用的 ComfyUI 工作流。用户要生成图片、改图或生成视频，或提到 ComfyUI 时，"
    "先调用本工具。不要用 shell、python、curl 直接请求 ComfyUI 的 HTTP 接口。"
)
INSPECT_DESCRIPTION = "查看一个 ComfyUI 工作流的参数名、类型、约束与产物类型"
EXECUTE_DESCRIPTION = (
    "提交一次 ComfyUI 工作流执行，是调用 ComfyUI 的唯一入口；提交成功后立即返回，"
    "产物完成后自动发送到当前会话"
)

WORKFLOW_PROPERTY = {
    "type": "string",
    "description": "工作流名称，与 comfy_list 返回的 name 一致",
}

LIST_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

INSPECT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"workflow": WORKFLOW_PROPERTY},
    "required": ["workflow"],
    "additionalProperties": False,
}

EXECUTE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "workflow": WORKFLOW_PROPERTY,
        "arguments": {
            "type": "object",
            "description": "按该工作流的参数契约传入；不需要参数时可省略",
        },
    },
    "required": ["workflow"],
    "additionalProperties": False,
}


class WorkflowRejected(RuntimeError):
    """入口拒绝本次调用，消息面向用户。"""


class WorkflowToolAdapter:
    """把模型调用接到清单、契约、执行与后台交付上。"""

    def __init__(
        self,
        access: AccessController,
        executor: WorkflowExecutor,
        gate: ConcurrencyGate,
        jobs: BackgroundJobs,
        catalog: dict[str, ToolContract],
        output_dir: Path,
    ) -> None:
        """记录入口需要的依赖。

        Args:
            access: 访问控制器。
            executor: 工作流执行器。
            gate: 入口并发名额。
            jobs: 后台交付任务。
            catalog: 工作流名称到契约的映射。
            output_dir: 产物落盘目录。
        """
        self.access = access
        self.executor = executor
        self.gate = gate
        self.jobs = jobs
        self.catalog = catalog
        self.output_dir = output_dir

    @property
    def has_workflows(self) -> bool:
        """是否有可用的工作流。"""
        return bool(self.catalog)

    async def list_workflows(self, event: AstrMessageEvent) -> str:
        """列出已启用的工作流。

        Args:
            event: 触发本次调用的消息事件。

        Returns:
            含名称与说明的 JSON 文本。

        Raises:
            WorkflowRejected: 准入未通过。
        """
        self._admit(event)
        items = [
            {"name": contract.name, "description": contract.description}
            for _, contract in sorted(self.catalog.items())
        ]
        return json.dumps(items, ensure_ascii=False, indent=2)

    async def inspect_workflow(self, event: AstrMessageEvent, workflow: str) -> str:
        """返回一个工作流对模型公开的契约。

        Args:
            event: 触发本次调用的消息事件。
            workflow: 工作流名称。

        Returns:
            契约视图的 JSON 文本。

        Raises:
            WorkflowRejected: 准入未通过。
            ContractError: 工作流不存在。
        """
        self._admit(event)
        contract = self._contract(workflow)
        graph = read_workflow(contract.workflow_path)
        problems = input_target_errors(contract, graph, contract.path.name)
        if problems:
            raise ContractError("\n".join(problems))
        return json.dumps(
            contract_view(contract, graph),
            ensure_ascii=False,
            indent=2,
        )

    async def execute_workflow(
        self,
        event: AstrMessageEvent,
        workflow: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """校验参数、提交执行，并在后台等待与交付产物。

        Args:
            event: 触发本次调用的消息事件。
            workflow: 工作流名称。
            arguments: 模型给出的参数，缺省为空。

        Returns:
            给模型的提交结果。

        Raises:
            WorkflowRejected: 准入、敏感词、并发或冷却未通过。
            ContractError: 工作流不存在或参数不合法。
            ComfyError: 提交失败或提交结果未知。
        """
        self._admit(event)
        contract = self._contract(workflow)
        values = _arguments(arguments)
        validate_arguments(contract, values)
        self._check_sensitive(contract, event, values)

        if not self.gate.try_acquire():
            logger.warning(f"[ComfyUI] {contract.name} 并发已满，拒绝本次调用")
            raise WorkflowRejected(f"⚠️ 并发已满（上限 {self.gate.limit}），请稍后重试")

        try:
            passed, remain = self.access.check_cooldown(event)
            if not passed:
                logger.info(f"[ComfyUI] 用户 {event.get_sender_id()} 冷却中")
                raise WorkflowRejected(f"⚠️ 冷却中，请在 {remain} 秒后重试")
            prompt_id = await self.executor.submit(
                contract,
                values,
                Attachments.from_event(event),
            )
        except BaseException:
            self.gate.release()
            raise

        task = self.jobs.start(
            self._deliver(contract, prompt_id, event),
            f"{contract.name}:{prompt_id}",
        )
        task.add_done_callback(self._release_slot)
        logger.info(
            f"[ComfyUI] 已排队后台交付 {contract.name} | prompt_id: {prompt_id}"
        )
        return f"已提交工作流 {contract.name}，正在后台生成，完成后会自动发送到当前会话"

    def _admit(self, event: AstrMessageEvent) -> None:
        """只放行白名单群与未被全局锁定的会话。

        Args:
            event: 触发本次调用的消息事件。

        Raises:
            WorkflowRejected: 准入未通过。
        """
        allowed, reason = self.access.check_access(event)
        if not allowed:
            raise WorkflowRejected(reason)

    def _contract(self, name: str) -> ToolContract:
        """按名称取出一个启用契约。

        Args:
            name: 工作流名称。

        Returns:
            对应的工具契约。

        Raises:
            ContractError: 名称未对应任何启用契约。
        """
        contract = self.catalog.get(name)
        if contract is None:
            available = ", ".join(sorted(self.catalog)) or "无"
            raise ContractError(f"未知的工作流: {name}，可用: {available}")
        return contract

    def _check_sensitive(
        self,
        contract: ToolContract,
        event: AstrMessageEvent,
        arguments: dict[str, Any],
    ) -> None:
        """检查本次调用的自由文本参数。

        Args:
            contract: 工具契约。
            event: 触发本次调用的消息事件。
            arguments: 本次调用的参数。

        Raises:
            WorkflowRejected: 命中敏感词。
        """
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

    async def _deliver(
        self,
        contract: ToolContract,
        prompt_id: str,
        event: AstrMessageEvent,
    ) -> None:
        """在后台等到结果并把产物发到原会话。

        Args:
            contract: 工具契约。
            prompt_id: 已提交的任务 ID。
            event: 触发本次调用的消息事件。
        """
        try:
            result = await self.executor.collect(contract, prompt_id)
            await self._deliver_artifacts(event, result)
        except ExecutionTimeout:
            logger.warning(f"[ComfyUI] {contract.name} 等待超时 | {prompt_id}")
            await send_text(
                event,
                f"⚠️ 工作流 {contract.name} 等待超时，执行状态未知",
            )
        except ExecutionUnknown as exc:
            logger.warning(f"[ComfyUI] {contract.name} 执行状态未知: {exc}")
            await send_text(event, f"⚠️ 工作流 {contract.name} 执行状态未知：{exc}")
        except ComfyError as exc:
            logger.warning(f"[ComfyUI] {contract.name} 执行失败: {exc}")
            await send_text(event, f"❌ 工作流 {contract.name} 执行失败：{exc}")
        except Exception as exc:
            logger.error(
                f"[ComfyUI] {contract.name} 后台交付失败: {exc}",
                exc_info=True,
            )
            await send_text(event, f"❌ 工作流 {contract.name} 交付失败：{exc}")

    def _release_slot(self, task: asyncio.Task[None]) -> None:
        """后台任务结束后归还本次执行占用的并发名额。

        Args:
            task: 已结束的后台任务。
        """
        self.gate.release()

    async def _store(self, artifact: FileArtifact) -> Path:
        """把一个文件类产物写到落盘目录。

        Args:
            artifact: 已经下载完成的文件类产物。

        Returns:
            落盘后的产物路径。
        """
        path = self.output_dir / f"{uuid.uuid4().hex}{artifact.suffix}"
        await asyncio.to_thread(path.write_bytes, artifact.content)
        return path

    async def _deliver_artifacts(
        self,
        event: AstrMessageEvent,
        result: ExecutionResult,
    ) -> None:
        """把每个正式产物各自作为一条消息发到原会话。

        Args:
            event: 触发本次调用的消息事件。
            result: 一次执行的结果。
        """
        for artifact in result.artifacts:
            if isinstance(artifact, TextArtifact):
                await send_text(event, artifact.text)
                continue
            path = await self._store(artifact)
            await send_chain(
                event,
                artifact_components(path, artifact.type, artifact.filename),
            )
        logger.info(
            f"[ComfyUI] 已发送 {len(result.artifacts)} 个产物到当前会话"
        )


def build_function_tools(
    adapter: WorkflowToolAdapter,
    module_path: str,
) -> list[FunctionTool]:
    """构建清单、查看、执行三个固定工具。

    Args:
        adapter: 工具调用的入口。
        module_path: 注册方模块路径，AstrBot 卸载插件时按它回收工具。

    Returns:
        可直接交给 ``Context.add_llm_tools`` 的工具列表；没有工作流时为空。
    """
    if not adapter.has_workflows:
        return []

    specs = (
        (LIST_NAME, LIST_DESCRIPTION, LIST_PARAMETERS, adapter.list_workflows),
        (
            INSPECT_NAME,
            INSPECT_DESCRIPTION,
            INSPECT_PARAMETERS,
            adapter.inspect_workflow,
        ),
        (
            EXECUTE_NAME,
            EXECUTE_DESCRIPTION,
            EXECUTE_PARAMETERS,
            adapter.execute_workflow,
        ),
    )

    built: list[FunctionTool] = []
    for name, description, parameters, handler in specs:
        function_tool = FunctionTool(
            name=name,
            description=description,
            parameters=parameters,
            handler=_guard(name, handler),
        )
        function_tool.__module__ = module_path
        built.append(function_tool)
    return built


def _guard(name: str, handler: ToolHandler) -> ToolHandler:
    """把入口异常转换成返回给模型的文本。

    Args:
        name: 工具名，出现在日志里。
        handler: 真实入口。

    Returns:
        捕获预期异常后的工具处理函数。
    """

    async def guarded(event: AstrMessageEvent, **arguments: Any) -> str:
        try:
            return await handler(event, **arguments)
        except WorkflowRejected as exc:
            logger.info(f"[ComfyUI] {name} 拒绝本次调用: {exc}")
            return str(exc)
        except MediaError as exc:
            logger.warning(f"[ComfyUI] {name} 附件引用无法解析: {exc}")
            return f"⚠️ {exc}"
        except (ContractError, ComfyError) as exc:
            logger.warning(f"[ComfyUI] {name} 失败: {exc}")
            return f"❌ {exc}"

    return guarded


def _arguments(raw: dict[str, Any] | None) -> dict[str, Any]:
    """把模型给出的参数规整成字典。

    Args:
        raw: 模型给出的参数。

    Returns:
        参数字典。

    Raises:
        ContractError: 参数不是对象。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ContractError(f"参数 arguments 必须是对象，实际为 {type(raw).__name__}")
    return raw


def _free_texts(contract: ToolContract, arguments: dict[str, Any]) -> list[str]:
    """取出需要检查敏感词的自由文本参数。

    Args:
        contract: 工具契约。
        arguments: 本次调用的参数。

    Returns:
        需要检查的文本。
    """
    texts: list[str] = []
    for name, parameter in contract.inputs.items():
        if parameter.type != "string" or parameter.enum:
            continue
        value = arguments.get(name)
        if isinstance(value, str) and value:
            texts.append(value)
    return texts
