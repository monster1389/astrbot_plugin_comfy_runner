"""命令实现：参数解析、文案与面向用户的流程。"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult

from .access import AccessController, group_id, is_group_message, self_id
from .comfy import ComfyError
from .config import POLICIES, Config
from .contract import (
    CONTRACT_SUFFIX,
    ContractError,
    read_workflow,
    remote_target_errors,
)
from .delivery import (
    chain_result,
    forward_components,
    image_components,
    text_result,
)
from .draw import Drawer, DrawError
from .plugin_state import WorkflowSelection
from .prompt import extract_lora_control_tags
from .registry import ContractReport, inspect_contracts
from .workflow import is_aux_file


@dataclass
class CommandContext:
    """命令需要的依赖。"""

    cfg: Config
    access: AccessController
    api: Any
    drawer: Drawer
    selection: WorkflowSelection
    context: Any
    plugin_dir: Path


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


async def check_contracts(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """检查全部工具契约（含禁用契约）并报告定位。"""
    user_id = str(event.get_sender_id())
    if user_id not in ctx.access.admin_ids:
        yield event.plain_result("🚫 权限不足，仅管理员可检查工具契约")
        return

    remote = "remote" in command_arguments(event).lower().split()
    reports = inspect_contracts(ctx.cfg.workflow_dir, ctx.plugin_dir)
    if remote:
        reports = await _apply_remote_check(ctx, reports)

    enabled = sum(1 for report in reports if report.enabled)
    lines = [
        "工具契约检查",
        f"目录: {ctx.cfg.workflow_dir}",
        f"共 {len(reports)} 个契约文件，启用 {enabled} 个",
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

    yield event.plain_result("\n".join(lines))


def _report_header(report: ContractReport) -> str:
    contract = report.contract
    if contract is None:
        return f"❌ {report.path.name} 无法读取"
    state = "启用" if contract.enabled else "禁用"
    mark = "✅" if report.ok else "❌"
    workflow = contract.workflow_path.name
    return f"{mark} {report.path.name} -> {contract.name} [{state}] 工作流 {workflow}"


async def _apply_remote_check(
    ctx: CommandContext,
    reports: list[ContractReport],
) -> list[ContractReport]:
    """用 ComfyUI 节点定义核对参数映射的目标字段。"""
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
        for parameter in contract.inputs.values():
            node = graph.get(parameter.node)
            if isinstance(node, dict) and node.get("class_type"):
                class_types.add(str(node["class_type"]))

    node_defs: dict[str, dict[str, Any]] = {}
    for class_type in sorted(class_types):
        try:
            node_defs[class_type] = await ctx.api.client.object_info(class_type)
        except ComfyError as exc:
            node_defs[class_type] = {}
            logger.warning(f"[ComfyUI] 读取节点定义失败 {class_type}: {exc}")

    checked: list[ContractReport] = []
    for report in reports:
        contract = report.contract
        graph = graphs.get(report.path)
        if contract is None or graph is None:
            checked.append(report)
            continue
        errors = remote_target_errors(contract, graph, node_defs)
        checked.append(
            replace(report, errors=report.errors + tuple(errors)) if errors else report
        )
    return checked


async def help_command(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """显示帮助与当前状态。"""
    allowed, reason = ctx.access.check_access(event)
    if not allowed:
        yield event.plain_result(reason)
        return

    gid = group_id(event)
    policy = ctx.access.policy_for(event)
    user_id = str(event.get_sender_id())
    is_admin = user_id in ctx.access.admin_ids

    tips = [
        "ComfyUI Pro 插件帮助",
        "",
        "",
        "【基础指令】",
        "  /画图 <提示词>     生成图片（转发模式）",
        "  /画图no <提示词>   生成图片（直发模式）",
        "  /comfy help        显示此帮助",
        "",
        "【LLM 模式】",
        "  直接对话：'帮我画一个可爱的猫娘'",
        "",
    ]

    if is_admin:
        tips.extend(
            [
                "【管理员指令】 ",
                "  /comfy ls          查看工作流",
                "  /comfy check       检查工具契约",
                "  /comfy save        导入工作流或契约",
                "  /comfy lock on|off 切换全局锁定",
                "  /comfy policy      设置群违禁级别",
                "",
            ]
        )

    tips.append("")
    tips.append(f"当前位置：{'群聊 ' + gid if gid else '私聊'}")
    tips.append(f"违禁级别：{policy}")
    tips.append(f"冷却时间：{ctx.cfg.control.cooldown_seconds} 秒")
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
        yield event.plain_result("⚠该指令仅支持在群聊中使用")
        return

    user_id = str(event.get_sender_id())
    if user_id not in ctx.access.admin_ids:
        yield event.plain_result("🚫 权限不足，仅管理员可修改违禁级别")
        return

    level_text = command_arguments(event)
    gid = group_id(event) or "未知"

    if not level_text:
        current = ctx.access.policy_for(event)
        yield event.plain_result(
            f"本群当前违禁级别：{current}\n"
            f"\n"
            f"可选级别：\n"
            f"  none - 不过滤\n"
            f"  lite - 轻度过滤\n"
            f"  full - 完全过滤\n"
            f"\n"
            f"用法：/comfy policy <级别>"
        )
        return

    level = level_text.split()[0].lower()
    if level not in POLICIES:
        yield event.plain_result("❌ 无效级别，可选：none / lite / full")
        return

    ctx.access.set_policy(gid, level)
    logger.info(f"[ComfyUI] 群 {gid} 违禁级别已设为 {level}（操作者：{user_id}）")
    yield event.plain_result(f"✅ 已将本群违禁级别设置为：{level}")


async def toggle_lockdown(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """管理员动态切换全局锁定状态"""
    user_id = str(event.get_sender_id())
    if user_id not in ctx.access.admin_ids:
        yield event.plain_result("🚫 权限不足，仅管理员可切换全局锁定")
        return

    if not ctx.cfg.control.lockdown_command_enabled:
        yield event.plain_result(
            "⚠锁定命令开关已关闭，请在插件配置中启用 control.lockdown_command_enabled"
        )
        return

    args = command_arguments(event).split()
    action = args[0].lower() if args else "status"

    if action in ("status", "状态", "查询"):
        yield event.plain_result(
            "全局锁定状态\n"
            "\n"
            f"当前: {'开启' if ctx.access.lockdown else '关闭'}\n"
            f"命令开关: "
            f"{'开启' if ctx.cfg.control.lockdown_command_enabled else '关闭'}\n"
            "用法: /comfy lock on|off|status"
        )
        return

    if action in ("on", "true", "1", "enable", "start", "开启"):
        ctx.access.lockdown = True
        logger.warning(f"[ComfyUI] 管理员 {user_id} 通过命令开启全局锁定")
        yield event.plain_result("已开启全局锁定：当前仅超级管理员可用绘图功能")
        return

    if action in ("off", "false", "0", "disable", "stop", "关闭"):
        ctx.access.lockdown = False
        logger.info(f"[ComfyUI] 管理员 {user_id} 通过命令关闭全局锁定")
        yield event.plain_result("已关闭全局锁定：恢复正常访问控制")
        return

    yield event.plain_result("❌ 参数无效，用法：/comfy lock on|off|status")


async def list_workflows(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """列出工作流与契约的配对、启用与校验状态。"""
    user_id = str(event.get_sender_id())
    if user_id not in ctx.access.admin_ids:
        yield event.plain_result("🚫 权限不足，仅管理员可查看工作流列表")
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

    lines = ["工作流与契约", f"目录: {workflow_dir}", ""]
    handled: set[Path] = set()
    for name in files:
        contract_path = workflow_dir / f"{Path(name).stem}{CONTRACT_SUFFIX}"
        handled.add(contract_path)
        lines.append(_workflow_line(name, reports.get(contract_path)))

    for path in sorted(reports):
        if path in handled:
            continue
        lines.append(_orphan_line(reports[path]))

    if len(lines) == 3:
        lines.append("目录中没有工作流文件")
    lines.append("")
    lines.append("新增或修正契约后用 /comfy check 查看细节")
    yield event.plain_result("\n".join(lines))


def _workflow_line(name: str, report: ContractReport | None) -> str:
    """渲染一行工作流与契约的配对状态。"""
    if report is None:
        return f"⚠ {name} 无配套契约"
    contract = report.contract
    if contract is None:
        return f"❌ {name} 契约 {report.path.name} 无法读取"
    state = "启用" if contract.enabled else "禁用"
    mark = "✅" if report.ok else "❌"
    problems = f"（{len(report.errors)} 个问题）" if report.errors else ""
    return f"{mark} {name} -> {contract.name} [{state}]{problems}"


def _orphan_line(report: ContractReport) -> str:
    """渲染一行找不到配对工作流的契约。"""
    if report.contract is not None:
        workflow_name = report.contract.workflow_path.name
        tool_name = report.contract.name
        state = "启用" if report.contract.enabled else "禁用"
        return (
            f"❌ {report.path.name} -> {tool_name} [{state}] "
            f"缺少工作流 {workflow_name}"
        )
    return f"❌ {report.path.name} 无法读取，且没有同名工作流"


async def save_workflow(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """保存/导入工作流"""
    user_id = str(event.get_sender_id())
    if user_id not in ctx.access.admin_ids:
        yield event.plain_result("🚫 权限不足，仅管理员可导入工作流")
        return

    content = command_arguments(event).split(None, 1)

    if len(content) < 2:
        yield event.plain_result(
            "❌ 参数不足\n"
            "用法：/comfy save <文件名> <JSON内容>\n"
            '示例：/comfy save my_workflow.json {"1":{...}}'
        )
        return

    filename = content[0]
    json_str = content[1]

    if not filename.endswith(".json"):
        filename += ".json"

    try:
        json_str = json_str.replace("```json", "").replace("```", "").strip()
        json_data = json.loads(json_str)
    except json.JSONDecodeError as e:
        yield event.plain_result(f"❌ JSON 解析失败：{str(e)[:50]}")
        return

    save_path = ctx.api.workflow.directory / filename

    try:
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        yield event.plain_result(f"❌ 保存失败: {e}")
        return

    ctx.selection.sync_options(ctx.api.workflow.list_files())
    logger.info(f"[ComfyUI] 管理员 {user_id} 导入工作流: {filename}")
    yield event.plain_result(
        f"✅ 保存成功！\n文件：{filename}\n使用 /comfy ls 查看状态"
    )


def command_prompt(event: AstrMessageEvent, strip_at: bool = False) -> str:
    """取出命令后面跟的提示词。

    Args:
        event: 消息事件。
        strip_at: 是否先去掉引用某人的 ``[At:数字]`` 标记。

    Returns:
        提示词，命令后没有内容时为空串。
    """
    text = (getattr(event, "message_str", "") or "").strip()
    if strip_at:
        text = re.sub(r"\[At:\d+\]\s*", "", text).strip()
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def paint(
    ctx: CommandContext,
    event: AstrMessageEvent,
    direct_send: bool,
    strip_at: bool = False,
    usage: str | None = None,
) -> AsyncIterator[MessageEventResult]:
    """处理画图的核心逻辑"""
    prompt = command_prompt(event, strip_at)
    if not prompt:
        message = usage or "❌ 请输入提示词，例如：/画图 1girl, smile"
        ctx.drawer.failures.remember(event, message, source="用户指令")
        yield text_result(event, message)
        return

    prompt, lora_selections = extract_lora_control_tags(prompt)
    try:
        path = await ctx.drawer.draw(event, prompt, lora_selections, "用户指令")
    except DrawError as exc:
        yield text_result(event, str(exc))
        return

    yield chain_result(event, image_chain(event, path, direct_send))


def image_chain(event: AstrMessageEvent, path: Path, direct_send: bool) -> list[Any]:
    """按发送模式把图片包成直发组件或转发节点。

    Args:
        event: 消息事件。
        path: 图片路径。
        direct_send: 是否直发。

    Returns:
        消息组件列表。
    """
    if direct_send:
        return image_components(path)
    return forward_components(path, self_id(event) or "0")


async def draw_tool(
    ctx: CommandContext,
    event: AstrMessageEvent,
    prompt: str = None,
    text: str = None,
    direct_send: bool = False,
) -> AsyncIterator[MessageEventResult]:
    """ComfyUI 文生图工具"""
    draw_source = event.get_extra("comfy_draw_source") or "LLM 工具"

    if not prompt and text:
        prompt = text

    if not prompt:
        message = "❌ 未提供 prompt，请重试"
        ctx.drawer.failures.remember(event, message, source=draw_source)
        yield message
        return

    if not isinstance(prompt, str) or not prompt.strip():
        raw = getattr(event, "message_str", "") or ""
        prompt = re.sub(r"```math\s*At:\d+```\s*", "", raw).strip()
        if not prompt:
            message = "❌ 请输入提示词"
            ctx.drawer.failures.remember(event, message, source=draw_source)
            yield message
            return

    prompt, lora_selections = extract_lora_control_tags(prompt)
    try:
        path = await ctx.drawer.draw(event, prompt, lora_selections, draw_source)
    except DrawError as exc:
        yield str(exc)
        return

    yield chain_result(event, image_chain(event, path, direct_send))
