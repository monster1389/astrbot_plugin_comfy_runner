"""命令实现：参数解析、文案与面向管理员的文件操作。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult

from .access import AccessController, group_id, is_group_message, scoped_id
from .comfy import ComfyClient, ComfyError
from .config import POLICIES, Config
from .contract import (
    CONTRACT_SUFFIX,
    ContractError,
    ToolContract,
    read_workflow,
    remote_target_errors,
)
from .interactive import InteractiveManager
from .paths import is_aux_file
from .registry import ContractReport, inspect_contracts


@dataclass(frozen=True, slots=True)
class CommandContext:
    """命令需要的依赖。"""

    cfg: Config
    access: AccessController
    client: ComfyClient
    plugin_dir: Path
    contracts: tuple[ToolContract, ...]
    interactive: InteractiveManager

    @property
    def workflow_names(self) -> tuple[str, ...]:
        """全部可用工作流名。"""
        return tuple(contract.name for contract in self.contracts)


def command_arguments(event: AstrMessageEvent, depth: int = 2) -> str:
    """取出子命令后面跟的全部文本。

    Args:
        event: 消息事件。
        depth: 要跳过的词数，``comfy <子命令>`` 为 2。

    Returns:
        参数文本，没有参数时为空串。
    """
    text = (getattr(event, "message_str", "") or "").strip()
    parts = text.split(None, depth)
    return parts[depth].strip() if len(parts) > depth else ""


async def help_command(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """显示命令清单与当前访问状态。"""
    allowed, reason = ctx.access.check_access(event)
    if not allowed:
        yield event.plain_result(reason)
        return

    gid = group_id(event)
    policy = ctx.access.policy_for(event)
    user_id = str(event.get_sender_id())
    is_admin = ctx.access.is_admin(event)

    tips = [
        "ComfyUI 工作流工具",
        "",
        "直接对话说出需求即可，模型会自己发现并调用对模型可见的工作流；",
        "也可使用 /comfy run 手动逐轮输入参数直接执行（不经过模型）。",
        "",
        "【指令】",
        "  /comfy help         显示此帮助",
        "  /comfy ls           查看可用工作流",
        "  /comfy run [序号或工作流] 手动触发工作流",
        "  /comfy cancel [任务ID] 取消参数收集或指定任务",
    ]

    if is_admin:
        tips.extend(
            [
                "",
                "【管理员指令】",
                "  /comfy check [remote] 检查工具契约",
                "  /comfy reload         重新加载工具契约",
                "  /comfy lock on|off    切换全局锁定",
                "  /comfy policy         设置群违禁级别",
            ]
        )

    tips.append("")
    tips.append(f"可用工作流：{', '.join(ctx.workflow_names) or '无'}")
    location = (
        f"群聊 {scoped_id(event, gid)}"
        if gid
        else f"私聊 {scoped_id(event, user_id)}"
    )
    tips.append(f"当前位置：{location}")
    tips.append(f"违禁级别：{policy}")
    tips.append(f"冷却时间：{ctx.cfg.control.cooldown_seconds} 秒")
    tips.append(f"并发上限：{ctx.cfg.execution.max_concurrency}")
    tips.append(f"全局锁定：{'开启' if ctx.access.lockdown else '关闭'}")
    if is_admin:
        tips.append("身份：管理员")
        tips.append(f"数据目录：{ctx.cfg.data_dir}")

    yield event.plain_result("\n".join(tips))


async def set_policy(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """查看或设置本群的违禁级别。"""
    allowed, reason = ctx.access.check_access(event)
    if not allowed:
        yield event.plain_result(reason)
        return

    if not is_group_message(event):
        yield event.plain_result("⚠️ 该指令仅支持在群聊中使用")
        return

    if not ctx.access.is_admin(event):
        yield event.plain_result("🚫 权限不足，仅管理员可修改违禁级别")
        return

    level_text = command_arguments(event)
    gid = group_id(event) or "未知"

    if not level_text:
        current = ctx.access.policy_for(event)
        yield event.plain_result(
            f"本群当前违禁级别：{current}\n"
            "\n"
            "可选级别：\n"
            "  none - 不过滤\n"
            "  lite - 轻度过滤\n"
            "  full - 完全过滤\n"
            "\n"
            "用法：/comfy policy <级别>"
        )
        return

    level = level_text.split()[0].lower()
    if level not in POLICIES:
        yield event.plain_result("❌ 无效级别，可选：none / lite / full")
        return

    policy_key = scoped_id(event, gid)
    actor = scoped_id(event, str(event.get_sender_id()))
    ctx.access.set_policy(policy_key, level)
    logger.info(f"[ComfyUI] 群 {policy_key} 违禁级别已设为 {level}（操作者：{actor}）")
    yield event.plain_result(f"✅ 已将本群违禁级别设置为：{level}")


async def toggle_lockdown(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """管理员动态切换全局锁定状态。"""
    if not ctx.access.is_admin(event):
        yield event.plain_result("🚫 权限不足，仅管理员可切换全局锁定")
        return

    if not ctx.cfg.control.lockdown_command_enabled:
        yield event.plain_result(
            "⚠️ 锁定命令开关已关闭，请在插件配置中启用 "
            "control.lockdown_command_enabled"
        )
        return

    args = command_arguments(event).split()
    action = args[0].lower() if args else "status"

    if action in ("status", "状态", "查询"):
        yield event.plain_result(
            "全局锁定状态\n"
            "\n"
            f"当前: {'开启' if ctx.access.lockdown else '关闭'}\n"
            "命令开关: "
            f"{'开启' if ctx.cfg.control.lockdown_command_enabled else '关闭'}\n"
            "用法: /comfy lock on|off|status"
        )
        return

    actor = scoped_id(event, str(event.get_sender_id()))
    if action in ("on", "true", "1", "enable", "start", "开启"):
        ctx.access.lockdown = True
        logger.warning(f"[ComfyUI] 管理员 {actor} 通过命令开启全局锁定")
        yield event.plain_result("✅ 已开启全局锁定：当前仅管理员可调用工作流工具")
        return

    if action in ("off", "false", "0", "disable", "stop", "关闭"):
        ctx.access.lockdown = False
        logger.info(f"[ComfyUI] 管理员 {actor} 通过命令关闭全局锁定")
        yield event.plain_result("✅ 已关闭全局锁定：恢复正常的访问控制")
        return

    yield event.plain_result("❌ 参数无效，用法：/comfy lock on|off|status")


async def list_workflows(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """列出可用工作流（普通用户看清单，管理员看文件与校验状态）。"""
    allowed, reason = ctx.access.check_access(event)
    if not allowed:
        yield event.plain_result(reason)
        return

    if not ctx.access.is_admin(event):
        yield event.plain_result(_user_workflow_list(ctx.contracts))
        return

    workflow_dir = ctx.cfg.workflow_dir
    if not workflow_dir.exists():
        yield event.plain_result("❌ 工作流目录不存在")
        return

    reports = {
        report.path: report
        for report in inspect_contracts(workflow_dir, ctx.plugin_dir)
    }
    files = sorted(
        path.name for path in workflow_dir.glob("*.json") if not is_aux_file(path.name)
    )

    run_index = {
        contract.name: index for index, contract in enumerate(ctx.contracts, start=1)
    }
    numbered: list[tuple[int, str]] = []
    others: list[str] = []
    handled: set[Path] = set()

    for name in files:
        contract_path = workflow_dir / f"{Path(name).stem}{CONTRACT_SUFFIX}"
        handled.add(contract_path)
        report = reports.get(contract_path)
        if (
            report is not None
            and report.ok
            and report.contract is not None
            and report.contract.name in run_index
        ):
            idx = run_index[report.contract.name]
            numbered.append((idx, f"{idx}. {_workflow_line(name, report)}"))
            continue
        if report is not None and report.ok and report.contract is not None:
            others.append(f"{_workflow_line(name, report)}（待 /comfy reload 生效）")
            continue
        others.append(_workflow_line(name, report))

    for path in sorted(reports):
        if path in handled:
            continue
        others.append(_orphan_line(reports[path]))

    lines = ["工作流与契约", f"目录: {workflow_dir}", ""]
    numbered.sort(key=lambda item: item[0])
    lines.extend(line for _, line in numbered)
    if numbered and others:
        lines.append("")
    lines.extend(others)
    if not numbered and not others:
        lines.append("目录中没有工作流文件")
    lines.append("")
    lines.append(
        "发送 /comfy run [序号或工作流名] 可直接调用；用 /comfy check 查看细节"
    )
    yield event.plain_result("\n".join(lines))


async def run_workflow_command(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """手动触发工作流并在不经过模型的情况下逐轮收集参数。"""
    query = command_arguments(event)
    reply = await ctx.interactive.start_run(event, ctx.contracts, query)
    yield event.plain_result(reply)


async def cancel_workflow_command(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """取消指定任务，或在不在向导中且无参数时告知用户。"""
    allowed, reason = ctx.access.check_access(event)
    if not allowed:
        yield event.plain_result(reason)
        return
    target = command_arguments(event)
    if not target:
        yield event.plain_result("⚠️ 当前没有进行中的工作流向导")
        return
    reply = await ctx.interactive.adapter.cancel_task_by_command(event, target)
    yield event.plain_result(reply)


def _user_workflow_list(contracts: tuple[ToolContract, ...]) -> str:
    if not contracts:
        return "当前没有可用的工作流"
    lines = ["可用工作流：", ""]
    for index, contract in enumerate(contracts, start=1):
        scope = "模型可见" if contract.model_visible else "仅命令"
        lines.append(f"{index}. {contract.name} [{scope}]：{contract.description}")
    lines.extend(["", "发送 /comfy run [序号或工作流名] 可直接调用。"])
    return "\n".join(lines)


def _visibility_label(contract: ToolContract) -> str:
    return "模型可见" if contract.model_visible else "仅命令"


def _workflow_line(name: str, report: ContractReport | None) -> str:
    """渲染一行工作流与契约的配对状态。"""
    if report is None:
        return f"⚠️ {name} 无配套契约"
    contract = report.contract
    if contract is None:
        return f"❌ {name} 契约 {report.path.name} 无法读取"
    state = _visibility_label(contract)
    mark = "✅" if report.ok else "❌"
    problems = f"（{len(report.errors)} 个问题）" if report.errors else ""
    return f"{mark} {name} -> {contract.name} [{state}]{problems}"


def _orphan_line(report: ContractReport) -> str:
    """渲染一行找不到配对工作流的契约。"""
    contract = report.contract
    if contract is None:
        return f"❌ {report.path.name} 无法读取，且没有同名工作流"
    workflow_name = contract.workflow_path.name
    state = _visibility_label(contract)
    return (
        f"❌ {report.path.name} -> {contract.name} [{state}] "
        f"缺少工作流 {workflow_name}"
    )


async def check_contracts(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """检查全部工具契约并报告定位。"""
    if not ctx.access.is_admin(event):
        yield event.plain_result("🚫 权限不足，仅管理员可检查工具契约")
        return

    remote = "remote" in command_arguments(event).lower().split()
    reports = inspect_contracts(ctx.cfg.workflow_dir, ctx.plugin_dir)
    if remote:
        reports = await _apply_remote_check(ctx, reports)

    model_visible = sum(1 for report in reports if report.model_visible)
    lines = [
        "工具契约检查",
        f"目录: {ctx.cfg.workflow_dir}",
        f"共 {len(reports)} 个契约文件，模型可见 {model_visible} 个",
    ]
    if remote:
        lines.append("已附带 ComfyUI 节点定义核对")
    lines.append("")

    if not reports:
        lines.append("目录中没有契约文件")
        yield event.plain_result("\n".join(lines))
        return

    for report in reports:
        lines.append(_report_header(report))
        lines.extend(f"  {error}" for error in report.errors)
        hidden = _hidden_line(report)
        if hidden:
            lines.append(hidden)

    yield event.plain_result("\n".join(lines))


def _report_header(report: ContractReport) -> str:
    contract = report.contract
    if contract is None:
        return f"❌ {report.path.name} 无法读取"
    state = _visibility_label(contract)
    mark = "✅" if report.ok else "❌"
    total = len(contract.inputs)
    visible = sum(1 for item in contract.inputs.values() if item.visible)
    return (
        f"{mark} {report.path.name} -> {contract.name} [{state}] "
        f"工作流 {contract.workflow_path.name} | "
        f"参数 {total}（对外 {visible}，隐藏 {total - visible}）"
    )


def _hidden_line(report: ContractReport) -> str:
    contract = report.contract
    if contract is None:
        return ""
    names = sorted(name for name, item in contract.inputs.items() if not item.visible)
    return f"  隐藏参数: {', '.join(names)}" if names else ""


async def reload_contracts_command(
    ctx: CommandContext,
    reload_fn: Callable[[], tuple[str, ...]],
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """重新读取并校验全部工具契约，在内存中刷新运行状态。

    Args:
        ctx: 命令上下文。
        reload_fn: 执行热刷新的回调，返回当前可用工作流名。
        event: 消息事件。
    """
    if not ctx.access.is_admin(event):
        yield event.plain_result("🚫 权限不足，仅管理员可重新加载工具契约")
        return

    try:
        names = reload_fn()
    except ContractError as exc:
        yield event.plain_result(f"❌ 重新加载失败，保持原运行状态:\n{exc}")
        return

    yield event.plain_result(
        f"✅ 已重新加载工具契约，当前可用：{', '.join(names) or '无'}"
    )


async def _apply_remote_check(
    ctx: CommandContext,
    reports: list[ContractReport],
) -> list[ContractReport]:
    """用 ComfyUI 节点定义核对参数与产物的映射目标。"""
    graphs: dict[Path, dict[str, Any]] = {}
    class_types: set[str] = set()
    for report in reports:
        contract = report.contract
        if contract is None:
            continue
        try:
            graphs[report.path] = read_workflow(contract.workflow_path)
        except ContractError:
            continue
        graph = graphs[report.path]
        node_ids = [parameter.node for parameter in contract.inputs.values()]
        node_ids.extend(output.node for output in contract.outputs.values())
        for node_id in node_ids:
            node = graph.get(node_id)
            if isinstance(node, dict) and node.get("class_type"):
                class_types.add(str(node["class_type"]))

    node_defs: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    for class_type in sorted(class_types):
        try:
            node_defs[class_type] = await ctx.client.object_info(class_type)
        except ComfyError as exc:
            failures[class_type] = str(exc)
            logger.warning(f"[ComfyUI] 读取节点定义失败 {class_type}: {exc}")

    checked: list[ContractReport] = []
    for report in reports:
        contract = report.contract
        graph = graphs.get(report.path)
        if contract is None or graph is None:
            checked.append(report)
            continue
        errors = remote_target_errors(contract, graph, node_defs, failures)
        checked.append(
            replace(report, errors=report.errors + tuple(errors)) if errors else report
        )
    return checked
