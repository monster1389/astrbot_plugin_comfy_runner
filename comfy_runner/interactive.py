"""手动命令交互向导：不经过模型的多轮工作流参数收集与提交。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.utils.session_waiter import (
    FILTERS,
    USER_SESSIONS,
    SessionController,
    SessionFilter,
    SessionWaiter,
)

from .comfy import ComfyError
from .contract import (
    ContractError,
    ParameterMapping,
    ToolContract,
    input_target_errors,
    parameter_defaults,
    read_workflow,
    validate_parameter_value,
)
from .delivery import send_text
from .media import KIND_LABELS, Attachments, MediaError
from .tools import WorkflowRejected, WorkflowToolAdapter

CANCEL_KEYWORDS = frozenset({"取消", "cancel", "/comfy cancel", "退出", "exit"})
SKIP_KEYWORDS = frozenset({"跳过", "skip"})
RUN_NOW_KEYWORDS = frozenset({"立即执行", "执行", "run"})
TRUE_KEYWORDS = frozenset({"true", "1", "yes", "y", "on", "是", "开", "开启"})
FALSE_KEYWORDS = frozenset({"false", "0", "no", "n", "off", "否", "关", "关闭"})
TYPE_LABELS = {
    "string": "文本",
    "integer": "整数",
    "number": "数字",
    "boolean": "布尔",
    "image": "图片",
    "video": "视频",
    "audio": "语音",
    "file": "文件",
}


def session_key(event: AstrMessageEvent) -> str:
    """生成隔离到会话来源与发送者的交互会话键。

    Args:
        event: 消息事件。

    Returns:
        会话键字符串。
    """
    return f"{event.unified_msg_origin}:{event.get_sender_id()}"


class UserSessionFilter(SessionFilter):
    """按会话来源与发送者隔离 AstrBot SessionWaiter。"""

    def filter(self, event: AstrMessageEvent) -> str:
        """返回与当前用户绑定的会话标识符。"""
        return session_key(event)


@dataclass(slots=True)
class _WizardState:
    """单个用户一次多轮命令调用的内部进度。"""

    contracts: tuple[ToolContract, ...]
    contract: ToolContract | None = None
    params: tuple[ParameterMapping, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)
    index: int = 0
    values: dict[str, Any] = field(default_factory=dict)
    attachments: dict[str, list[Any]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """用于提示语的工作流名称。"""
        return f"工作流 {self.contract.name}" if self.contract else "工作流选择"

    @property
    def remaining_all_optional(self) -> bool:
        """当前及剩余参数是否全部为可选。"""
        return all(not item.required for item in self.params[self.index :])


class InteractiveManager:
    """管理不经过模型的多轮工作流命令向导。"""

    def __init__(
        self,
        adapter: WorkflowToolAdapter,
        timeout_seconds: int,
    ) -> None:
        """记录提交入口与交互空闲超时。

        Args:
            adapter: 共享的准入、敏感词与提交适配器。
            timeout_seconds: 每一轮等待用户输入的超时秒数。
        """
        self.adapter = adapter
        self.timeout_seconds = timeout_seconds
        self._sessions: dict[str, tuple[SessionWaiter, asyncio.Task[None]]] = {}

    def is_active(self, event: AstrMessageEvent) -> bool:
        """判断当前发送者是否正处于未完成的向导中。

        Args:
            event: 消息事件。

        Returns:
            已有活跃会话时为 True。
        """
        session = USER_SESSIONS.get(session_key(event))
        return bool(session and not session.session_controller.future.done())

    async def cancel_all(self) -> None:
        """插件卸载时结束所有尚未完成的交互向导。"""
        items = list(self._sessions.values())
        self._sessions.clear()
        for waiter, task in items:
            waiter.session_controller.stop()
            task.cancel()
        if items:
            await asyncio.gather(*(task for _, task in items), return_exceptions=True)

    async def start_run(
        self,
        event: AstrMessageEvent,
        contracts: tuple[ToolContract, ...],
        query: str,
    ) -> str:
        """处理 /comfy run 命令，按情况立即提交或进入多轮收集。

        Args:
            event: 触发命令的消息事件。
            contracts: 当前可用的全部工作流契约。
            query: 命令后附带的工作流名称，空串表示先选工作流。

        Returns:
            给用户的即时回复文本。
        """
        try:
            self.adapter.admit(event)
        except WorkflowRejected as exc:
            return str(exc)

        if self.is_active(event):
            return (
                "⚠️ 当前已有正在进行的工作流向导，"
                "请继续输入参数，或发送「取消」中止"
            )

        if not contracts:
            return "⚠️ 当前没有可用的工作流"

        target = query.strip()
        if not target:
            state = _WizardState(contracts=contracts)
            await self._launch_session(event, state)
            return _format_workflow_list(contracts)

        contract = next((item for item in contracts if item.name == target), None)
        if contract is None:
            available = ", ".join(item.name for item in contracts)
            return f"❌ 未找到工作流：{target}\n可用工作流：{available}"

        try:
            params, defaults = _prepare_contract(contract)
        except ContractError as exc:
            return f"❌ {exc}"

        if not params:
            return await self._submit(event, contract, {}, Attachments(items={}))

        state = _WizardState(
            contracts=contracts,
            contract=contract,
            params=params,
            defaults=defaults,
        )
        await self._launch_session(event, state)
        return _format_param_prompt(state)

    async def _launch_session(
        self,
        event: AstrMessageEvent,
        state: _WizardState,
    ) -> None:
        key = session_key(event)
        session_filter = UserSessionFilter()
        FILTERS.append(session_filter)
        waiter = SessionWaiter(session_filter, key, False)

        async def handler(
            controller: SessionController,
            turn_event: AstrMessageEvent,
        ) -> None:
            await self._handle_turn(controller, turn_event, state)

        task = asyncio.create_task(
            self._run_waiter(
                key,
                waiter,
                handler,
                event,
                lambda: state.label,
            )
        )
        self._sessions[key] = (waiter, task)
        await asyncio.sleep(0)

    async def _run_waiter(
        self,
        key: str,
        waiter: SessionWaiter,
        handler: Any,
        origin_event: AstrMessageEvent,
        label_fn: Callable[[], str],
    ) -> None:
        try:
            await waiter.register_wait(handler, timeout=self.timeout_seconds)
        except TimeoutError:
            await send_text(
                origin_event,
                f"⚠️ 等待输入超时（{self.timeout_seconds} 秒），"
                f"已取消{label_fn()}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[ComfyUI] 交互向导异常: {exc}", exc_info=True)
            await send_text(origin_event, f"❌ 交互向导异常：{exc}")
        finally:
            self._sessions.pop(key, None)

    async def _handle_turn(
        self,
        controller: SessionController,
        event: AstrMessageEvent,
        state: _WizardState,
    ) -> None:
        controller.keep(self.timeout_seconds, reset_timeout=True)
        text = (getattr(event, "message_str", "") or "").strip()
        lowered = text.lower()

        if lowered in CANCEL_KEYWORDS:
            controller.stop()
            await send_text(event, f"已取消{state.label}")
            return

        if lowered.startswith(("/comfy run", "comfy run")):
            await send_text(
                event,
                "⚠️ 当前已有正在进行的工作流向导，"
                "请继续输入当前内容，或发送「取消」中止",
            )
            return

        if state.contract is None:
            await self._handle_selection(controller, event, state, text)
            return

        if state.remaining_all_optional and lowered in RUN_NOW_KEYWORDS:
            await self._finish_and_submit(controller, event, state)
            return

        param = state.params[state.index]
        if not param.required and lowered in SKIP_KEYWORDS:
            await self._advance(controller, event, state)
            return

        error = self._collect_parameter(event, state, param, text)
        if error is not None:
            await send_text(event, error)
            return

        await self._advance(controller, event, state)

    async def _handle_selection(
        self,
        controller: SessionController,
        event: AstrMessageEvent,
        state: _WizardState,
        text: str,
    ) -> None:
        contract = _pick_contract(state.contracts, text)
        if contract is None:
            await send_text(
                event,
                f"⚠️ 未找到工作流，请回复序号（1-{len(state.contracts)}）"
                "或工作流名称，或发送「取消」",
            )
            return

        try:
            params, defaults = _prepare_contract(contract)
        except ContractError as exc:
            controller.stop()
            await send_text(event, f"❌ {exc}")
            return

        state.contract = contract
        state.params = params
        state.defaults = defaults
        if not params:
            await self._finish_and_submit(controller, event, state)
            return

        await send_text(event, _format_param_prompt(state))

    def _collect_parameter(
        self,
        event: AstrMessageEvent,
        state: _WizardState,
        param: ParameterMapping,
        text: str,
    ) -> str | None:
        contract = state.contract
        assert contract is not None
        if param.is_reference:
            return self._collect_reference(event, state, param, text)

        try:
            value = _parse_scalar_input(param, text)
            validate_parameter_value(param, value)
            self.adapter.ensure_parameter_sensitive(
                param,
                event,
                contract.name,
                value,
            )
        except WorkflowRejected as exc:
            return str(exc)
        except (ValueError, ContractError) as exc:
            return f"⚠️ {exc}"

        state.values[param.name] = value
        return None

    def _collect_reference(
        self,
        event: AstrMessageEvent,
        state: _WizardState,
        param: ParameterMapping,
        text: str,
    ) -> str | None:
        label = KIND_LABELS[param.type]
        turn_attachments = Attachments.from_event(event)
        components = turn_attachments.items.get(param.type, ())
        if components:
            bucket = state.attachments.setdefault(param.type, [])
            bucket.append(components[0])
            state.values[param.name] = f"attachment:{len(bucket)}"
            return None

        if text.startswith(("http://", "https://")):
            suffix = Path(urlparse(text).path).suffix
            if not suffix:
                return (
                    f"⚠️ 链接缺少文件扩展名，请发送{label}、"
                    f"回复带{label}的消息或带扩展名的 http(s) 链接"
                )
            state.values[param.name] = text
            return None

        return (
            f"⚠️ 命令模式仅支持直接发送{label}、回复带{label}的消息或 "
            "http(s) 链接，不支持本地路径，请重新输入"
        )

    async def _advance(
        self,
        controller: SessionController,
        event: AstrMessageEvent,
        state: _WizardState,
    ) -> None:
        state.index += 1
        if state.index >= len(state.params):
            await self._finish_and_submit(controller, event, state)
            return
        await send_text(event, _format_param_prompt(state))

    async def _finish_and_submit(
        self,
        controller: SessionController,
        event: AstrMessageEvent,
        state: _WizardState,
    ) -> None:
        assert state.contract is not None
        controller.stop()
        attachments = Attachments(
            items={kind: tuple(items) for kind, items in state.attachments.items()}
        )
        reply = await self._submit(event, state.contract, state.values, attachments)
        await send_text(event, reply)

    async def _submit(
        self,
        event: AstrMessageEvent,
        contract: ToolContract,
        values: dict[str, Any],
        attachments: Attachments,
    ) -> str:
        try:
            message = await self.adapter.submit_contract(
                event,
                contract,
                values,
                attachments,
            )
            return f"✅ {message}"
        except WorkflowRejected as exc:
            logger.info(f"[ComfyUI] 命令调用被拒绝 {contract.name}: {exc}")
            return str(exc)
        except MediaError as exc:
            logger.warning(f"[ComfyUI] 命令调用附件解析失败 {contract.name}: {exc}")
            return f"⚠️ {exc}"
        except (ContractError, ComfyError) as exc:
            logger.warning(f"[ComfyUI] 命令调用失败 {contract.name}: {exc}")
            return f"❌ {exc}"


def _prepare_contract(
    contract: ToolContract,
) -> tuple[tuple[ParameterMapping, ...], dict[str, Any]]:
    graph = read_workflow(contract.workflow_path)
    problems = input_target_errors(contract, graph, contract.path.name)
    if problems:
        raise ContractError("\n".join(problems))
    params = tuple(param for param in contract.inputs.values() if param.visible)
    defaults = parameter_defaults(contract, graph)
    return params, defaults


def _pick_contract(
    contracts: tuple[ToolContract, ...],
    text: str,
) -> ToolContract | None:
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(contracts):
            return contracts[index - 1]
        return None
    return next((item for item in contracts if item.name == text), None)


def _parse_scalar_input(param: ParameterMapping, text: str) -> Any:
    if param.enum and text.isdigit():
        index = int(text)
        if 1 <= index <= len(param.enum):
            return param.enum[index - 1]

    if param.type == "string":
        if not text:
            raise ValueError("请输入文本内容")
        return text

    if param.type == "integer":
        try:
            return int(text)
        except ValueError as exc:
            raise ValueError("请输入整数") from exc

    if param.type == "number":
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError("请输入数字") from exc
        if not math.isfinite(value):
            raise ValueError("请输入有限数字")
        return value

    if param.type == "boolean":
        lowered = text.lower()
        if lowered in TRUE_KEYWORDS:
            return True
        if lowered in FALSE_KEYWORDS:
            return False
        raise ValueError("请输入布尔值（true/false、是/否、1/0、on/off、开/关）")

    raise ValueError(f"不支持的参数类型: {param.type}")


def _format_workflow_list(contracts: tuple[ToolContract, ...]) -> str:
    lines = ["请选择要执行的工作流：", ""]
    for index, contract in enumerate(contracts, start=1):
        lines.append(f"{index}. {contract.name} - {contract.description}")
    lines.extend(["", "请回复序号或工作流名称，发送「取消」中止。"])
    return "\n".join(lines)


def _format_param_prompt(state: _WizardState) -> str:
    param = state.params[state.index]
    total = len(state.params)
    kind = TYPE_LABELS.get(param.type, param.type)
    req = "必填" if param.required else "可选"
    lines = [
        f"[{state.index + 1}/{total}] {param.name}（{kind}，{req}）",
        f"说明：{param.description}",
    ]

    if param.enum:
        lines.append("可选值（回复序号或原值）：")
        for idx, item in enumerate(param.enum, start=1):
            lines.append(f"  {idx}. {item}")
    elif param.type == "boolean":
        lines.append("可选值：true/false、是/否、1/0、on/off、开/关")
    elif param.is_reference:
        label = KIND_LABELS[param.type]
        lines.append(f"请直接发送{label}、回复带{label}的消息，或输入 http(s) 链接")

    constraints = _format_constraints(param)
    if constraints:
        lines.append(f"约束：{constraints}")

    if not param.required:
        if param.name in state.defaults:
            lines.append(f"工作流原值：{state.defaults[param.name]}")
        hint = "发送「跳过」保留原值"
        if state.remaining_all_optional:
            hint += "，或发送「立即执行」直接提交"
        lines.append(hint)

    lines.append("发送「取消」中止。")
    return "\n".join(lines)


def _format_constraints(param: ParameterMapping) -> str:
    parts: list[str] = []
    if param.min_length is not None:
        parts.append(f"最短 {param.min_length} 字符")
    if param.max_length is not None:
        parts.append(f"最长 {param.max_length} 字符")
    if param.minimum is not None:
        parts.append(f"最小 {param.minimum}")
    if param.maximum is not None:
        parts.append(f"最大 {param.maximum}")
    return "，".join(parts)
