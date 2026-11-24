"""工作流工具入口：清单、契约查看、提交执行、任务管理与后台交付。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.pipeline.process_stage import follow_up

from .access import AccessController, scoped_id
from .background import BackgroundJobs
from .comfy import ComfyError, ExecutionTimeout, ExecutionUnknown
from .contract import (
    ContractError,
    ParameterMapping,
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
    extract_execution_result,
)
from .media import (
    Attachments,
    MediaError,
    MediaPayload,
    parse_task_reference,
    resolve_reference,
)
from .paths import cleanup_temp_artifact, resolve_temp_path
from .tasks import TERMINAL_TASK_STATUSES, StoredArtifact, TaskRecord, TaskStore

ToolHandler = Callable[..., Awaitable[str]]

LIST_NAME = "comfy_list"
INSPECT_NAME = "comfy_inspect"
EXECUTE_NAME = "comfy_execute"
TASK_NAME = "comfy_task"
TOOL_NAMES = (LIST_NAME, INSPECT_NAME, EXECUTE_NAME, TASK_NAME)

TASK_ACTIONS = ("status", "wait", "download", "deliver", "cancel")
WAIT_ABORT_SLICE_SECONDS = 0.25

LIST_DESCRIPTION = (
    "列出可用的 ComfyUI 工作流。用户要生成图片、改图或生成视频，或提到 ComfyUI 时，"
    "先调用本工具。不要用 shell、python、curl 直接请求 ComfyUI 的 HTTP 接口。"
)
INSPECT_DESCRIPTION = "查看一个 ComfyUI 工作流的参数名、类型、约束与产物类型"
EXECUTE_DESCRIPTION = (
    "提交一次 ComfyUI 工作流执行，是调用 ComfyUI 的唯一入口；提交成功后立即返回"
    "任务 ID，可根据 deliver 参数决定完成后是否自动发送到当前会话"
)
TASK_DESCRIPTION = (
    "查询、等待、下载、发送或取消工作流任务。不要用 shell、python、curl "
    "查询 ComfyUI 队列历史或手动发送产物。"
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
        "deliver": {
            "type": "boolean",
            "description": (
                "完成后是否自动将产物发送到当前会话，默认为 true；"
                "批量生成或作为下游工作流前置输入时可设为 false"
            ),
            "default": True,
        },
    },
    "required": ["workflow"],
    "additionalProperties": False,
}

TASK_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(TASK_ACTIONS),
            "description": (
                "操作类型：status 查看并发与任务状态；wait 等待任务完成；"
                "download 将产物下载到临时目录并返回路径；"
                "deliver 将任务产物发送到当前会话；cancel 取消队列或运行中的任务"
            ),
        },
        "prompt_id": {
            "type": "string",
            "description": (
                "目标任务 ID（或逗号分隔的多个任务 ID，用于 wait/cancel）；"
                "action 为 status 时可省略以列出最近任务"
            ),
        },
        "index": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "可选，指定第几个产物（从 1 开始），用于 download 或 deliver"
            ),
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


class WorkflowRejected(RuntimeError):
    """入口拒绝本次调用，消息面向用户。"""


@dataclass(slots=True)
class _ActiveJob:
    task_id: str
    contract: ToolContract
    task: asyncio.Task[None] | None
    done_event: asyncio.Event
    depends_on: tuple[str, ...] = ()
    prompt_id: str | None = None


class WorkflowToolAdapter:
    """把模型调用接到清单、契约、执行、任务账本与后台交付上。"""

    def __init__(
        self,
        access: AccessController,
        executor: WorkflowExecutor,
        gate: ConcurrencyGate,
        jobs: BackgroundJobs,
        catalog: dict[str, ToolContract],
        temp_dir: Path,
        tasks: TaskStore,
        all_contracts: dict[str, ToolContract] | None = None,
    ) -> None:
        """记录入口需要的依赖。

        Args:
            access: 访问控制器。
            executor: 工作流执行器。
            gate: 入口并发名额。
            jobs: 后台交付任务。
            catalog: 对模型可见的工作流契约映射。
            temp_dir: 产物临时目录。
            tasks: 持久化任务账本。
            all_contracts: 全部可用工作流契约映射。
        """
        self.access = access
        self.executor = executor
        self.gate = gate
        self.jobs = jobs
        self.catalog = catalog
        self.temp_dir = temp_dir
        self.tasks = tasks
        self.all_contracts = (
            all_contracts if all_contracts is not None else dict(catalog)
        )
        self._active_jobs: dict[str, _ActiveJob] = {}

    @property
    def has_workflows(self) -> bool:
        """是否有对模型可见的工作流。"""
        return bool(self.catalog)

    async def list_workflows(self, event: AstrMessageEvent) -> str:
        """列出对模型可见的工作流。

        Args:
            event: 触发本次调用的消息事件。

        Returns:
            含名称与说明的 JSON 文本。

        Raises:
            WorkflowRejected: 准入未通过。
        """
        self.admit(event)
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
        self.admit(event)
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
        deliver: bool = True,
    ) -> str:
        """校验参数、提交执行，并在后台等待与按需交付产物。

        Args:
            event: 触发本次调用的消息事件。
            workflow: 工作流名称。
            arguments: 模型给出的参数，缺省为空。
            deliver: 完成后是否自动发送到当前会话。

        Returns:
            给模型的提交结果。

        Raises:
            WorkflowRejected: 准入、敏感词、并发或冷却未通过。
            ContractError: 工作流不存在或参数不合法。
            ComfyError: 提交失败或提交结果未知。
        """
        self.admit(event)
        if not isinstance(deliver, bool):
            raise ContractError("参数 deliver 必须是布尔值")
        contract = self._contract(workflow)
        values = _arguments(arguments)
        return await self.submit_contract(
            event,
            contract,
            values,
            Attachments.from_event(event),
            deliver=deliver,
        )

    async def submit_contract(
        self,
        event: AstrMessageEvent,
        contract: ToolContract,
        values: dict[str, Any],
        attachments: Attachments,
        deliver: bool = True,
    ) -> str:
        """校验参数、准入与并发后提交指定契约执行，并按需后台交付。

        Args:
            event: 触发本次调用的消息事件。
            contract: 目标工具契约。
            values: 调用参数字典。
            attachments: 本次调用可用的附件集合。
            deliver: 完成后是否自动将产物发到当前会话。

        Returns:
            已提交状态说明。

        Raises:
            WorkflowRejected: 准入、敏感词、并发或冷却未通过。
            ContractError: 参数不合法或工作流映射有误。
            MediaError: 引用解析或上传失败。
            ComfyError: 提交失败或提交结果未知。
        """
        self.admit(event)
        validate_arguments(contract, values)
        self._check_sensitive(contract, event, values)

        graph = read_workflow(contract.workflow_path)
        problems = input_target_errors(contract, graph, contract.path.name)
        if problems:
            raise ContractError("\n".join(problems))

        pending_upstreams = await self._validate_task_dependencies(contract, values)

        if not self.gate.try_acquire():
            logger.warning(f"[ComfyUI] {contract.name} 并发已满，拒绝本次调用")
            raise WorkflowRejected(f"⚠️ 并发已满（上限 {self.gate.limit}），请稍后重试")

        sender_key = scoped_id(event, str(event.get_sender_id()))
        origin = getattr(event, "unified_msg_origin", "") or ""

        try:
            passed, remain = self.access.check_cooldown(event)
            if not passed:
                logger.info(f"[ComfyUI] 用户 {event.get_sender_id()} 冷却中")
                raise WorkflowRejected(f"⚠️ 冷却中，请在 {remain} 秒后重试")
            if pending_upstreams:
                pre_uploaded = await self._upload_non_task_references(
                    contract,
                    values,
                    attachments,
                )
                temp_id = f"pending-{uuid.uuid4().hex[:12]}"
                self.tasks.create_task(
                    prompt_id=temp_id,
                    workflow=contract.name,
                    sender_id=sender_key,
                    origin=origin,
                    arguments=values,
                    status="waiting_dependency",
                    deliver_requested=deliver,
                    delivery_status="pending" if deliver else "skipped",
                )
                return self._start_dependency_job(
                    temp_id,
                    contract,
                    values,
                    attachments,
                    pre_uploaded,
                    pending_upstreams,
                    event,
                    deliver,
                )
            prompt_id = await self.executor.submit(
                contract,
                values,
                attachments,
                task_resolver=self._resolve_task_reference,
            )
        except BaseException:
            self.gate.release()
            raise

        self.tasks.create_task(
            prompt_id=prompt_id,
            workflow=contract.name,
            sender_id=sender_key,
            origin=origin,
            arguments=values,
            status="queued",
            deliver_requested=deliver,
            delivery_status="pending" if deliver else "skipped",
        )
        job = _ActiveJob(
            task_id=prompt_id,
            contract=contract,
            task=None,
            done_event=asyncio.Event(),
            prompt_id=prompt_id,
        )
        self._active_jobs[prompt_id] = job
        task = self.jobs.start(
            self._deliver_existing(contract, prompt_id, event, deliver, job),
            f"{contract.name}:{prompt_id}",
        )
        job.task = task
        task.add_done_callback(self._release_slot)
        logger.info(
            f"[ComfyUI] 已排队后台交付 {contract.name} | prompt_id: {prompt_id}"
        )
        if deliver:
            return (
                f"已提交工作流 {contract.name}（任务: {prompt_id}），"
                "正在后台生成，完成后会自动发送到当前会话"
            )
        return (
            f"已提交工作流 {contract.name}（任务: {prompt_id}），"
            "正在后台生成，完成后不自动发送，可通过 comfy_task 查看、下载或发送"
        )

    async def manage_task(
        self,
        event: AstrMessageEvent,
        action: str,
        prompt_id: str | None = None,
        index: int | None = None,
    ) -> str:
        """执行任务账本查询、等待、下载、补发与取消。

        Args:
            event: 触发本次调用的消息事件。
            action: 操作类型。
            prompt_id: 目标任务 ID 或逗号分隔的任务 ID 列表。
            index: 可选的产物序号（从 1 开始）。

        Returns:
            操作结果文本或 JSON。

        Raises:
            WorkflowRejected: 准入或取消权限未通过。
            ContractError: 参数不合法或任务不存在。
            ComfyError: 与 ComfyUI 交互失败。
        """
        self.admit(event)
        if action not in TASK_ACTIONS:
            allowed = ", ".join(TASK_ACTIONS)
            raise ContractError(f"不支持的任务操作 {action!r}，可选: {allowed}")
        if index is not None and (
            isinstance(index, bool) or not isinstance(index, int) or index < 1
        ):
            raise ContractError("参数 index 必须是大于等于 1 的整数")

        if action == "status":
            return await self._action_status(prompt_id)
        ids = _parse_prompt_ids(prompt_id)
        if action == "wait":
            return await self._action_wait(event, ids)
        if action == "cancel":
            return await self._action_cancel(event, ids)
        if len(ids) != 1:
            raise ContractError(f"操作 {action} 只支持单个任务 ID")
        if action == "download":
            return await self._action_download(ids[0], index)
        return await self._action_deliver(event, ids[0], index)

    async def cancel_task_by_command(
        self,
        event: AstrMessageEvent,
        raw_ids: str,
    ) -> str:
        """处理 /comfy cancel <任务ID> 命令入口。

        Args:
            event: 触发命令的消息事件。
            raw_ids: 命令参数里的任务 ID 字符串。

        Returns:
            面向用户的状态文本。
        """
        try:
            self.admit(event)
            ids = _parse_prompt_ids(raw_ids.replace(" ", ","), allow_space=True)
            results = await self._cancel_ids(event, ids)
        except WorkflowRejected as exc:
            return str(exc)
        except (ContractError, ComfyError) as exc:
            return f"❌ {exc}"
        lines = ["✅ 任务取消结果："]
        for item in results:
            if item["cancelled"]:
                lines.append(f"- {item['prompt_id']}: 已取消")
            else:
                lines.append(f"- {item['prompt_id']}: {item['detail']}")
        return "\n".join(lines)

    def admit(self, event: AstrMessageEvent) -> None:
        """只放行白名单群与未被全局锁定的会话。

        Args:
            event: 触发本次调用的消息事件。

        Raises:
            WorkflowRejected: 准入未通过。
        """
        allowed, reason = self.access.check_access(event)
        if not allowed:
            raise WorkflowRejected(reason)

    def ensure_parameter_sensitive(
        self,
        parameter: ParameterMapping,
        event: AstrMessageEvent,
        workflow_name: str,
        value: Any,
    ) -> None:
        """检查单个自由文本参数是否触发敏感词。

        Args:
            parameter: 参数映射定义。
            event: 触发本次调用的消息事件。
            workflow_name: 工作流名称。
            value: 待检查的参数值。

        Raises:
            WorkflowRejected: 命中敏感词。
        """
        if parameter.type != "string" or parameter.enum:
            return
        if not isinstance(value, str) or not value:
            return
        passed, words = self.access.check_sensitive(value, event)
        if passed:
            return
        shown = "、".join(words[:5])
        more = f" 等 {len(words)} 个" if len(words) > 5 else ""
        logger.warning(
            f"[ComfyUI] 用户 {event.get_sender_id()} 触发敏感词: {shown}"
        )
        raise WorkflowRejected(
            f"🚫 检测到敏感词：{shown}{more}，无法执行 {workflow_name}"
        )

    def _contract(self, name: str) -> ToolContract:
        """按名称取出一个对模型可见的契约。"""
        contract = self.catalog.get(name)
        if contract is None:
            available = ", ".join(sorted(self.catalog)) or "无"
            raise ContractError(f"未知的工作流: {name}，可用: {available}")
        return contract

    def _contract_for_task(self, workflow_name: str) -> ToolContract:
        contract = (
            self.all_contracts.get(workflow_name)
            or self.catalog.get(workflow_name)
        )
        if contract is None:
            raise ContractError(f"找不到工作流契约: {workflow_name}")
        return contract

    def _check_sensitive(
        self,
        contract: ToolContract,
        event: AstrMessageEvent,
        arguments: dict[str, Any],
    ) -> None:
        for name, parameter in contract.inputs.items():
            if name in arguments:
                self.ensure_parameter_sensitive(
                    parameter,
                    event,
                    contract.name,
                    arguments[name],
                )

    async def _validate_task_dependencies(
        self,
        contract: ToolContract,
        values: dict[str, Any],
    ) -> list[str]:
        pending: list[str] = []
        for name, parameter in contract.inputs.items():
            if not parameter.is_reference or name not in values:
                continue
            parsed = parse_task_reference(str(values[name]))
            if parsed is None:
                continue
            upstream_id, artifact_index = parsed
            record = await self._get_or_sync_task(upstream_id)
            if record is None:
                raise MediaError(f"找不到上游任务 {upstream_id}")
            if record.status in ("failed", "cancelled", "unknown"):
                raise MediaError(
                    f"上游任务 {record.prompt_id} 状态为 {record.status}，"
                    "无法引用其产物"
                )
            if record.status == "succeeded":
                _pick_task_artifact(record, artifact_index, parameter.type)
                continue
            upstream_contract = self._contract_for_task(record.workflow)
            if not any(
                output.type == parameter.type
                for output in upstream_contract.outputs.values()
            ):
                raise MediaError(
                    f"上游任务 {record.prompt_id} ({record.workflow}) "
                    f"不产生 {parameter.type} 类型的产物"
                )
            if record.prompt_id not in pending:
                pending.append(record.prompt_id)
        return pending

    async def _upload_non_task_references(
        self,
        contract: ToolContract,
        values: dict[str, Any],
        attachments: Attachments,
    ) -> dict[str, str]:
        uploaded: dict[str, str] = {}
        for name, parameter in contract.inputs.items():
            if not parameter.is_reference or name not in values:
                continue
            if parse_task_reference(str(values[name])) is not None:
                continue
            payload = await resolve_reference(
                values[name],
                parameter.type,
                attachments,
            )
            uploaded[name] = await self.executor.client.upload(
                payload.filename,
                payload.content,
            )
        return uploaded

    def _start_dependency_job(
        self,
        temp_id: str,
        contract: ToolContract,
        values: dict[str, Any],
        attachments: Attachments,
        pre_uploaded: dict[str, str],
        pending_upstreams: list[str],
        event: AstrMessageEvent,
        deliver: bool,
    ) -> str:
        job = _ActiveJob(
            task_id=temp_id,
            contract=contract,
            task=None,
            done_event=asyncio.Event(),
            depends_on=tuple(pending_upstreams),
        )
        self._active_jobs[temp_id] = job
        task = self.jobs.start(
            self._run_with_dependencies(
                temp_id,
                contract,
                values,
                attachments,
                pre_uploaded,
                pending_upstreams,
                event,
                deliver,
                job,
            ),
            f"{contract.name}:{temp_id}",
        )
        job.task = task
        task.add_done_callback(self._release_slot)
        logger.info(
            f"[ComfyUI] 已排入前置依赖等待 {contract.name} | "
            f"任务: {temp_id} | 等待: {', '.join(pending_upstreams)}"
        )
        if deliver:
            return (
                f"已提交工作流 {contract.name}（任务: {temp_id}），"
                "正在等待前置任务完成后生成并发送到当前会话"
            )
        return (
            f"已提交工作流 {contract.name}（任务: {temp_id}），"
            "正在等待前置任务完成后生成，完成后不自动发送，可通过 comfy_task 查看"
        )

    async def _run_with_dependencies(
        self,
        temp_id: str,
        contract: ToolContract,
        values: dict[str, Any],
        attachments: Attachments,
        pre_uploaded: dict[str, str],
        pending_upstreams: list[str],
        event: AstrMessageEvent,
        deliver: bool,
        job: _ActiveJob,
    ) -> None:
        try:
            for upstream_id in pending_upstreams:
                ready = await self._wait_upstream_ready(upstream_id)
                if not ready:
                    upstream = self.tasks.get(upstream_id)
                    up_status = upstream.status if upstream else "missing"
                    if up_status == "cancelled":
                        self.tasks.update_status(temp_id, "cancelled", "前置任务已取消")
                        return
                    reason = f"前置任务 {upstream_id} 状态为 {up_status}"
                    self.tasks.update_status(temp_id, "failed", reason)
                    await self._safe_notify(
                        event,
                        f"❌ 工作流 {contract.name} 执行失败：{reason}",
                    )
                    return

            prompt_id = await self.executor.submit(
                contract,
                values,
                attachments,
                task_resolver=self._resolve_task_reference,
                pre_uploaded=pre_uploaded,
            )
            self.tasks.bind_prompt_id(temp_id, prompt_id)
            job.prompt_id = prompt_id
            self._active_jobs[prompt_id] = job
            await self._collect_and_deliver(contract, prompt_id, event, deliver)
        except asyncio.CancelledError:
            current = self.tasks.get(job.prompt_id or temp_id)
            if current is not None and current.status not in TERMINAL_TASK_STATUSES:
                self.tasks.update_status(current.prompt_id, "cancelled", "任务已取消")
            raise
        except (MediaError, ContractError, ComfyError) as exc:
            logger.warning(f"[ComfyUI] {contract.name} 执行失败: {exc}")
            self.tasks.update_status(job.prompt_id or temp_id, "failed", str(exc))
            await self._safe_notify(event, f"❌ 工作流 {contract.name} 执行失败：{exc}")
        except Exception as exc:
            logger.error(
                f"[ComfyUI] {contract.name} 后台任务异常: {exc}",
                exc_info=True,
            )
            self.tasks.update_status(job.prompt_id or temp_id, "failed", str(exc))
            await self._safe_notify(event, f"❌ 工作流 {contract.name} 执行失败：{exc}")
        finally:
            self._unregister_job(job)

    async def _wait_upstream_ready(self, upstream_id: str) -> bool:
        while True:
            record = self.tasks.get(upstream_id)
            if record is None:
                return False
            if record.status == "succeeded":
                return True
            if record.status in ("failed", "cancelled", "unknown"):
                return False
            up_job = (
                self._active_jobs.get(record.prompt_id)
                or self._active_jobs.get(upstream_id)
            )
            if up_job is not None:
                await up_job.done_event.wait()
                continue
            await self._sync_task(record.prompt_id)
            refreshed = self.tasks.get(record.prompt_id)
            if refreshed is None or refreshed.status in (
                "failed",
                "cancelled",
                "unknown",
            ):
                return False
            if refreshed.status == "succeeded":
                return True
            await asyncio.sleep(self.executor.client.poll_interval)

    async def _deliver_existing(
        self,
        contract: ToolContract,
        prompt_id: str,
        event: AstrMessageEvent,
        deliver: bool,
        job: _ActiveJob,
    ) -> None:
        try:
            await self._collect_and_deliver(contract, prompt_id, event, deliver)
        except asyncio.CancelledError:
            current = self.tasks.get(prompt_id)
            if current is not None and current.status not in TERMINAL_TASK_STATUSES:
                self.tasks.update_status(prompt_id, "cancelled", "任务已取消")
            raise
        finally:
            self._unregister_job(job)

    async def _collect_and_deliver(
        self,
        contract: ToolContract,
        prompt_id: str,
        event: AstrMessageEvent,
        deliver: bool,
    ) -> None:
        try:
            result = await self.executor.collect(contract, prompt_id)
        except ExecutionTimeout as exc:
            logger.warning(f"[ComfyUI] {contract.name} 等待超时 | {prompt_id}")
            self.tasks.update_status(prompt_id, "unknown", str(exc))
            await self._safe_notify(
                event,
                f"⚠️ 工作流 {contract.name} 等待超时，执行状态未知",
            )
            return
        except ExecutionUnknown as exc:
            logger.warning(f"[ComfyUI] {contract.name} 执行状态未知: {exc}")
            self.tasks.update_status(prompt_id, "unknown", str(exc))
            await self._safe_notify(
                event,
                f"⚠️ 工作流 {contract.name} 执行状态未知：{exc}",
            )
            return
        except ComfyError as exc:
            logger.warning(f"[ComfyUI] {contract.name} 执行失败: {exc}")
            self.tasks.update_status(prompt_id, "failed", str(exc))
            await self._safe_notify(
                event,
                f"❌ 工作流 {contract.name} 执行失败：{exc}",
            )
            return
        except Exception as exc:
            logger.error(
                f"[ComfyUI] {contract.name} 后台收集失败: {exc}",
                exc_info=True,
            )
            self.tasks.update_status(prompt_id, "failed", str(exc))
            await self._safe_notify(
                event,
                f"❌ 工作流 {contract.name} 交付失败：{exc}",
            )
            return

        stored = _to_stored_artifacts(result)
        self.tasks.record_success(
            prompt_id,
            stored,
            "pending" if deliver else "skipped",
        )
        if not deliver:
            return

        record = self.tasks.get(prompt_id)
        if record is None:
            return
        try:
            await self._send_task_artifacts(event, record, record.artifacts)
            self.tasks.update_delivery(prompt_id, "delivered")
        except Exception as exc:
            logger.error(
                f"[ComfyUI] {contract.name} 后台交付失败: {exc}",
                exc_info=True,
            )
            self.tasks.update_delivery(prompt_id, "failed", str(exc))
            await self._safe_notify(
                event,
                f"❌ 工作流 {contract.name} 交付失败：{exc}",
            )

    async def _send_task_artifacts(
        self,
        event: AstrMessageEvent,
        record: TaskRecord,
        artifacts: tuple[StoredArtifact, ...],
    ) -> None:
        for artifact in artifacts:
            if artifact.ref is None:
                await send_text(event, artifact.text or "")
                continue
            path = resolve_temp_path(
                self.temp_dir,
                artifact.ref.subfolder,
                artifact.ref.filename,
            )
            if not path.is_file():
                content = await self.executor.client.download(artifact.ref)
                await asyncio.to_thread(path.write_bytes, content)
            filename = Path(artifact.ref.filename).name
            await send_chain(
                event,
                artifact_components(path, artifact.type, filename),
            )
            latest = self.tasks.get(record.prompt_id)
            keep = any(
                item.index == artifact.index and item.downloaded
                for item in (latest.artifacts if latest else ())
            )
            if not keep:
                await asyncio.to_thread(cleanup_temp_artifact, self.temp_dir, path)
        logger.info(
            f"[ComfyUI] 已发送 {len(artifacts)} 个产物到当前会话"
        )

    async def _resolve_task_reference(
        self,
        task_id: str,
        artifact_index: int | None,
        expected_type: str,
    ) -> MediaPayload:
        record = await self._get_or_sync_task(task_id)
        if record is None:
            raise MediaError(f"找不到任务 {task_id}")
        if record.status != "succeeded":
            raise MediaError(
                f"任务 {record.prompt_id} 状态为 {record.status}，无法引用产物"
            )
        artifact = _pick_task_artifact(record, artifact_index, expected_type)
        assert artifact.ref is not None
        temp_path = resolve_temp_path(
            self.temp_dir,
            artifact.ref.subfolder,
            artifact.ref.filename,
        )
        if temp_path.is_file():
            content = await asyncio.to_thread(temp_path.read_bytes)
        else:
            content = await self.executor.client.download(artifact.ref)
            await asyncio.to_thread(cleanup_temp_artifact, self.temp_dir, temp_path)
        suffix = Path(artifact.ref.filename).suffix
        if not suffix:
            raise MediaError(f"产物 {artifact.ref.filename} 缺少文件扩展名")
        return MediaPayload(content=content, suffix=suffix)

    async def _action_status(self, prompt_id: str | None) -> str:
        concurrency = {
            "active": self.gate.active,
            "limit": self.gate.limit,
            "available": max(0, self.gate.limit - self.gate.active),
        }
        if not prompt_id or not prompt_id.strip():
            records = self.tasks.list_recent(limit=20)
            synced: list[dict[str, Any]] = []
            for record in records:
                latest = await self._sync_task(record.prompt_id)
                if latest is not None:
                    synced.append(_task_view(latest))
            return json.dumps(
                {"concurrency": concurrency, "tasks": synced},
                ensure_ascii=False,
                indent=2,
            )
        ids = _parse_prompt_ids(prompt_id)
        tasks_view: list[dict[str, Any]] = []
        for pid in ids:
            record = await self._get_or_sync_task(pid)
            if record is None:
                raise ContractError(f"找不到任务: {pid}")
            tasks_view.append(_task_view(record))
        payload: dict[str, Any] = {"concurrency": concurrency}
        if len(tasks_view) == 1:
            payload["task"] = tasks_view[0]
        else:
            payload["tasks"] = tasks_view
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def _action_wait(
        self,
        event: AstrMessageEvent,
        ids: list[str],
    ) -> str:
        resolved: list[str] = []
        for pid in ids:
            record = self.tasks.get(pid)
            if record is None:
                raise ContractError(f"找不到任务: {pid}")
            resolved.append(record.prompt_id)

        deadlines: dict[str, tuple[bool, float]] = {}
        timeout = self.executor.client.timeout
        poll_interval = self.executor.client.poll_interval
        now = time.monotonic()
        for pid in resolved:
            deadlines[pid] = (False, now + timeout)

        while True:
            reason = _follow_up_abort_reason(event)
            if reason:
                snapshots = [
                    _task_view(item)
                    for pid in resolved
                    if (item := self.tasks.get(pid)) is not None
                ]
                return json.dumps(
                    {"interrupted": True, "reason": reason, "tasks": snapshots},
                    ensure_ascii=False,
                    indent=2,
                )

            all_done = True
            for pid in resolved:
                current = await self._sync_task(pid)
                if current is None:
                    raise ContractError(f"找不到任务: {pid}")
                if current.status in TERMINAL_TASK_STATUSES:
                    continue
                all_done = False
                if current.status == "waiting_dependency":
                    continue
                if current.prompt_id in self._active_jobs:
                    continue
                running_started, deadline = deadlines[pid]
                if not running_started:
                    _, pending_ids = await self.executor.client.queue_state()
                    if current.prompt_id in pending_ids:
                        deadlines[pid] = (False, time.monotonic() + timeout)
                    else:
                        deadlines[pid] = (True, deadline)
                        running_started = True
                if running_started and time.monotonic() >= deadline:
                    self.tasks.update_status(
                        current.prompt_id,
                        "unknown",
                        f"生成超时（{timeout:g} 秒）",
                    )

            if all_done:
                snapshots = [
                    _task_view(item)
                    for pid in resolved
                    if (item := self.tasks.get(pid)) is not None
                ]
                return json.dumps(
                    {"interrupted": False, "tasks": snapshots},
                    ensure_ascii=False,
                    indent=2,
                )

            interrupted = await _sleep_wait_slice(poll_interval, event)
            if interrupted:
                snapshots = [
                    _task_view(item)
                    for pid in resolved
                    if (item := self.tasks.get(pid)) is not None
                ]
                return json.dumps(
                    {
                        "interrupted": True,
                        "reason": interrupted,
                        "tasks": snapshots,
                    },
                    ensure_ascii=False,
                    indent=2,
                )

    async def _action_download(self, prompt_id: str, index: int | None) -> str:
        record = await self._get_or_sync_task(prompt_id)
        if record is None:
            raise ContractError(f"找不到任务: {prompt_id}")
        if record.status != "succeeded":
            raise ContractError(
                f"任务 {record.prompt_id} 状态为 {record.status}，无法下载产物"
            )
        selected = _select_artifacts(record, index)
        items: list[dict[str, Any]] = []
        for artifact in selected:
            if artifact.ref is None:
                items.append(
                    {
                        "index": artifact.index,
                        "output": artifact.output,
                        "type": artifact.type,
                        "text": artifact.text,
                    }
                )
                continue
            path = resolve_temp_path(
                self.temp_dir,
                artifact.ref.subfolder,
                artifact.ref.filename,
            )
            if not path.is_file():
                content = await self.executor.client.download(artifact.ref)
                await asyncio.to_thread(path.write_bytes, content)
            self.tasks.mark_downloaded(record.prompt_id, artifact.index)
            items.append(
                {
                    "index": artifact.index,
                    "output": artifact.output,
                    "type": artifact.type,
                    "filename": artifact.ref.filename,
                    "subfolder": artifact.ref.subfolder,
                    "path": str(path),
                }
            )
        return json.dumps(
            {"prompt_id": record.prompt_id, "artifacts": items},
            ensure_ascii=False,
            indent=2,
        )

    async def _action_deliver(
        self,
        event: AstrMessageEvent,
        prompt_id: str,
        index: int | None,
    ) -> str:
        record = await self._get_or_sync_task(prompt_id)
        if record is None:
            raise ContractError(f"找不到任务: {prompt_id}")
        if record.status != "succeeded":
            raise ContractError(
                f"任务 {record.prompt_id} 状态为 {record.status}，无法发送产物"
            )
        selected = _select_artifacts(record, index)
        try:
            await self._send_task_artifacts(event, record, selected)
        except Exception as exc:
            self.tasks.update_delivery(record.prompt_id, "failed", str(exc))
            raise ComfyError(f"发送产物失败: {exc}") from exc
        self.tasks.update_delivery(record.prompt_id, "delivered")
        return json.dumps(
            {
                "prompt_id": record.prompt_id,
                "delivered": len(selected),
                "delivery_status": "delivered",
            },
            ensure_ascii=False,
            indent=2,
        )

    async def _action_cancel(
        self,
        event: AstrMessageEvent,
        ids: list[str],
    ) -> str:
        results = await self._cancel_ids(event, ids)
        concurrency = {
            "active": self.gate.active,
            "limit": self.gate.limit,
            "available": max(0, self.gate.limit - self.gate.active),
        }
        return json.dumps(
            {"results": results, "concurrency": concurrency},
            ensure_ascii=False,
            indent=2,
        )

    async def _cancel_ids(
        self,
        event: AstrMessageEvent,
        ids: list[str],
    ) -> list[dict[str, Any]]:
        records: list[TaskRecord] = []
        for pid in ids:
            record = self.tasks.get(pid)
            if record is None:
                raise ContractError(f"找不到任务: {pid}")
            self._ensure_can_cancel(event, record)
            records.append(record)
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for record in records:
            if record.prompt_id in seen:
                continue
            seen.add(record.prompt_id)
            results.append(await self._cancel_single_task(record.prompt_id))
        return results

    async def _cancel_single_task(self, prompt_id: str) -> dict[str, Any]:
        record = await self._get_or_sync_task(prompt_id)
        if record is None:
            raise ContractError(f"找不到任务: {prompt_id}")

        dependents = [
            job.task_id
            for job in list(self._active_jobs.values())
            if record.prompt_id in job.depends_on
        ]
        for dep_id in dependents:
            await self._cancel_single_task(dep_id)

        record = self.tasks.get(record.prompt_id) or record
        if record.status in ("succeeded", "failed", "cancelled"):
            return {
                "prompt_id": record.prompt_id,
                "status": record.status,
                "cancelled": False,
                "detail": f"任务已处于 {record.status} 状态，无需取消",
            }

        queue_action = "waiting_dependency"
        if record.status != "waiting_dependency":
            queue_action = await self.executor.client.cancel(record.prompt_id)
        self.tasks.update_status(record.prompt_id, "cancelled", "任务已取消")

        job = self._active_jobs.get(record.prompt_id)
        if job is not None and job.task is not None and not job.task.done():
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)

        return {
            "prompt_id": record.prompt_id,
            "status": "cancelled",
            "cancelled": True,
            "queue_action": queue_action,
        }

    def _ensure_can_cancel(self, event: AstrMessageEvent, record: TaskRecord) -> None:
        if self.access.is_admin(event):
            return
        caller = scoped_id(event, str(event.get_sender_id()))
        if caller != record.sender_id:
            raise WorkflowRejected("🚫 权限不足，仅任务提交者或管理员可取消该任务")

    async def _get_or_sync_task(self, prompt_id: str) -> TaskRecord | None:
        record = self.tasks.get(prompt_id)
        if record is None:
            return None
        return await self._sync_task(record.prompt_id)

    async def _sync_task(self, prompt_id: str) -> TaskRecord | None:
        record = self.tasks.get(prompt_id)
        if record is None:
            return None
        if record.status not in ("queued", "running", "unknown"):
            return record

        entry = await self.executor.client.history(record.prompt_id)
        if entry is not None:
            contract = self._contract_for_task(record.workflow)
            try:
                result = extract_execution_result(contract, record.prompt_id, entry)
            except ExecutionUnknown as exc:
                self.tasks.update_status(record.prompt_id, "unknown", str(exc))
            except ComfyError as exc:
                self.tasks.update_status(record.prompt_id, "failed", str(exc))
            else:
                delivery_status = (
                    record.delivery_status
                    if record.deliver_requested
                    else "skipped"
                )
                self.tasks.record_success(
                    record.prompt_id,
                    _to_stored_artifacts(result),
                    delivery_status,
                )
            return self.tasks.get(record.prompt_id)

        if record.status in ("queued", "running"):
            running_ids, pending_ids = await self.executor.client.queue_state()
            if record.prompt_id in running_ids and record.status != "running":
                self.tasks.update_status(record.prompt_id, "running")
            elif record.prompt_id in pending_ids and record.status != "queued":
                self.tasks.update_status(record.prompt_id, "queued")
        return self.tasks.get(record.prompt_id)

    def _release_slot(self, task: asyncio.Task[None]) -> None:
        self.gate.release()

    def _unregister_job(self, job: _ActiveJob) -> None:
        self._active_jobs.pop(job.task_id, None)
        if job.prompt_id:
            self._active_jobs.pop(job.prompt_id, None)
        job.done_event.set()

    async def _safe_notify(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await send_text(event, text)
        except Exception as exc:
            logger.warning(f"[ComfyUI] 发送通知消息失败: {exc}")


def build_function_tools(
    adapter: WorkflowToolAdapter,
    module_path: str,
) -> list[FunctionTool]:
    """构建清单、查看、执行、任务管理四个固定工具。

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
        (
            TASK_NAME,
            TASK_DESCRIPTION,
            TASK_PARAMETERS,
            adapter.manage_task,
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
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ContractError("参数 arguments 必须是对象")
    return dict(raw)


def _parse_prompt_ids(
    raw: str | None,
    allow_space: bool = False,
) -> list[str]:
    if not isinstance(raw, str) or not raw.strip():
        raise ContractError("缺少必填参数: prompt_id")
    text = raw.replace(" ", ",") if allow_space else raw
    items = [part.strip() for part in text.split(",") if part.strip()]
    if not items:
        raise ContractError("参数 prompt_id 不能为空")
    return items


def _to_stored_artifacts(result: ExecutionResult) -> tuple[StoredArtifact, ...]:
    items: list[StoredArtifact] = []
    for idx, artifact in enumerate(result.artifacts, start=1):
        if isinstance(artifact, TextArtifact):
            items.append(
                StoredArtifact(
                    index=idx,
                    output=artifact.output,
                    type="text",
                    text=artifact.text,
                )
            )
            continue
        assert isinstance(artifact, FileArtifact)
        items.append(
            StoredArtifact(
                index=idx,
                output=artifact.output,
                type=artifact.type,
                ref=artifact.ref,
            )
        )
    return tuple(items)


def _select_artifacts(
    record: TaskRecord,
    index: int | None,
) -> tuple[StoredArtifact, ...]:
    if not record.artifacts:
        raise ContractError(f"任务 {record.prompt_id} 没有可用产物")
    if index is None:
        return record.artifacts
    for item in record.artifacts:
        if item.index == index:
            return (item,)
    raise ContractError(
        f"任务 {record.prompt_id} 没有第 {index} 个产物"
        f"（共 {len(record.artifacts)} 个）"
    )


def _pick_task_artifact(
    record: TaskRecord,
    artifact_index: int | None,
    expected_type: str,
) -> StoredArtifact:
    if not record.artifacts:
        raise MediaError(f"任务 {record.prompt_id} 没有可用产物")
    if artifact_index is not None:
        match = next(
            (item for item in record.artifacts if item.index == artifact_index),
            None,
        )
        if match is None:
            raise MediaError(
                f"任务 {record.prompt_id} 没有第 {artifact_index} 个产物"
            )
        if match.type != expected_type or match.ref is None:
            raise MediaError(
                f"任务 {record.prompt_id} 的第 {artifact_index} 个产物"
                f"类型为 {match.type}，不能作为 {expected_type} 使用"
            )
        return match
    match = next(
        (
            item
            for item in record.artifacts
            if item.type == expected_type and item.ref is not None
        ),
        None,
    )
    if match is None:
        raise MediaError(
            f"任务 {record.prompt_id} 没有 {expected_type} 类型的文件产物"
        )
    return match


def _task_view(record: TaskRecord) -> dict[str, Any]:
    args_summary: dict[str, Any] = {}
    for key, value in record.arguments.items():
        if isinstance(value, str) and len(value) > 120:
            args_summary[key] = value[:117] + "..."
        else:
            args_summary[key] = value
    artifacts_view: list[dict[str, Any]] = []
    for item in record.artifacts:
        entry: dict[str, Any] = {
            "index": item.index,
            "output": item.output,
            "type": item.type,
        }
        if item.ref is not None:
            entry["filename"] = item.ref.filename
            entry["subfolder"] = item.ref.subfolder
            entry["downloaded"] = item.downloaded
        else:
            entry["text"] = item.text
        artifacts_view.append(entry)
    return {
        "prompt_id": record.prompt_id,
        "workflow": record.workflow,
        "status": record.status,
        "delivery_status": record.delivery_status,
        "deliver_requested": record.deliver_requested,
        "arguments": args_summary,
        "error": record.error,
        "created_at": round(record.created_at, 3),
        "updated_at": round(record.updated_at, 3),
        "artifacts": artifacts_view,
    }


def _follow_up_abort_reason(event: AstrMessageEvent) -> str | None:
    if event.get_extra("agent_stop_requested"):
        return "收到停止请求，已中断等待"
    umo = getattr(event, "unified_msg_origin", "")
    if not umo:
        return None
    state = follow_up._FOLLOW_UP_ORDER_STATE.get(umo)
    if not isinstance(state, dict):
        return None
    statuses = state.get("statuses")
    if isinstance(statuses, dict) and any(
        value == "pending" for value in statuses.values()
    ):
        return "检测到用户发来新消息，已中断等待以便先响应用户"
    return None


async def _sleep_wait_slice(
    duration: float,
    event: AstrMessageEvent,
) -> str | None:
    if duration <= WAIT_ABORT_SLICE_SECONDS:
        await asyncio.sleep(duration)
        return _follow_up_abort_reason(event)
    remaining = duration
    while remaining > 0:
        step = min(remaining, WAIT_ABORT_SLICE_SECONDS)
        await asyncio.sleep(step)
        reason = _follow_up_abort_reason(event)
        if reason:
            return reason
        remaining -= step
    return None
