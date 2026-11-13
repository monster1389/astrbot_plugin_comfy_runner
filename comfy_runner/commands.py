"""命令实现：参数解析、文案与面向用户的流程。"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import traceback
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.message.message_event_result import MessageChain

from .access import AccessController, group_id, is_group_message, self_id
from .config import POLICIES, Config
from .delivery import (
    chain_result,
    forward_components,
    image_components,
    send_chain,
    send_text,
    text_result,
)
from .draw import Drawer, DrawError
from .prompt import (
    build_segments,
    coerce_history,
    count_visible_text_length,
    extract_lora_control_tags,
    extract_usable_prompt_entries,
    find_pic_prompts,
    normalize_prompt_signature,
    parse_lora_picks,
    sanitize_context_content,
    strip_control_tags,
)
from .workflow import format_strength, is_aux_file, normalize_lora_key

FORCE_DRAW_CONTEXT_LIMIT = 12
FORCE_DRAW_MIN_REPLY_LENGTH = 100


class _ComfyImageMarker:
    """多图模式的图片占位标记，存储 prompt 信息，在 chain 中占位"""

    def __init__(self, prompt: str, index: int, lora_selections=None):
        self.prompt = prompt
        self.index = index
        self.lora_selections = lora_selections or []


@dataclass
class CommandContext:
    """命令需要的依赖。"""

    cfg: Config
    access: AccessController
    api: Any
    drawer: Drawer
    context: Any


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
        "  /comfy帮助         显示此帮助",
        "",
        "【LLM 模式】",
        "  直接对话：'帮我画一个可爱的猫娘'",
        "",
    ]

    if is_admin:
        tips.extend(
            [
                "【管理员指令】 ",
                "  /comfy_ls              列出所有工作流",
                "  /comfy_use <序号>      切换工作流",
                "  /comfy_save            导入新工作流",
                "  /comfy_add             步数覆盖（按节点ID）",
                "  /comfy_lock on|off     切换全局锁定",
                "  /违禁级别              设置群敏感度",
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

    full_msg = event.message_str.strip()
    parts = full_msg.split()
    gid = group_id(event) or "未知"

    if len(parts) == 1:
        current = ctx.access.policy_for(event)
        yield event.plain_result(
            f"本群当前违禁级别：{current}\n"
            f"\n"
            f"可选级别：\n"
            f"  none - 不过滤\n"
            f"  lite - 轻度过滤\n"
            f"  full - 完全过滤\n"
            f"\n"
            f"用法：/违禁级别 <级别>"
        )
        return

    level = parts[1].lower()
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

    args = event.message_str.split()
    action = args[1].lower() if len(args) > 1 else "status"

    if action in ("status", "状态", "查询"):
        yield event.plain_result(
            "全局锁定状态\n"
            "\n"
            f"当前: {'开启' if ctx.access.lockdown else '关闭'}\n"
            f"命令开关: "
            f"{'开启' if ctx.cfg.control.lockdown_command_enabled else '关闭'}\n"
            "用法: /comfy_lock on|off|status"
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

    yield event.plain_result("❌ 参数无效，用法：/comfy_lock on|off|status")


async def list_workflows(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """列出当前所有可用工作流"""
    user_id = str(event.get_sender_id())
    if user_id not in ctx.access.admin_ids:
        yield event.plain_result("🚫 权限不足，仅管理员可查看工作流列表")
        return

    if not ctx.api.workflow.directory.exists():
        yield event.plain_result("❌ 工作流目录不存在")
        return

    files = sorted(
        [
            f.name
            for f in ctx.api.workflow.directory.glob("*.json")
            if not is_aux_file(f.name)
        ]
    )

    if not files:
        yield event.plain_result("目录中没有工作流文件")
        return

    current_file = ctx.api.workflow.filename if ctx.api else "未知"

    msg = ["可用工作流列表", ""]

    for i, f in enumerate(files, 1):
        stem = Path(f).stem
        sidecar = ctx.api.workflow.directory / f"{stem}.steps.json"

        steps_info = ""
        if sidecar.exists():
            try:
                with open(sidecar, encoding="utf-8") as sf:
                    data = json.load(sf)
                    if data and isinstance(data, dict):
                        count = len(data)
                        steps_info = f" [覆盖:{count}项]"
            except Exception:
                pass

        if f == current_file:
            msg.append(f"✅ {i}. {f}{steps_info} (当前)")
        else:
            msg.append(f"   {i}. {f}{steps_info}")

    msg.append("")
    msg.append("")
    msg.append("切换：/comfy_use <序号>")
    msg.append("覆盖：/comfy_add <节点ID> <步数>")
    msg.append("查看：/comfy_add list")

    yield event.plain_result("\n".join(msg))


async def use_workflow(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """切换工作流"""
    user_id = str(event.get_sender_id())
    if user_id not in ctx.access.admin_ids:
        yield event.plain_result("🚫 权限不足，仅管理员可切换工作流")
        return

    args = event.message_str.split()
    if len(args) < 2:
        yield event.plain_result(
            "❌ 参数不足\n"
            "用法：/comfy_use <序号> [正面ID] [负面ID] [输出ID]\n"
            "示例：/comfy_use 1 6 7 9"
        )
        return

    try:
        files = sorted(
            [
                f.name
                for f in ctx.api.workflow.directory.glob("*.json")
                if not is_aux_file(f.name)
            ]
        )

        index = int(args[1])
        if not (1 <= index <= len(files)):
            yield event.plain_result(
                f"❌ 序号错误，请输入 1 到 {len(files)} 之间的数字"
            )
            return
        filename = files[index - 1]
    except ValueError:
        yield event.plain_result("❌ 请输入有效的数字序号")
        return
    except Exception as e:
        yield event.plain_result(f"❌ 查找工作流失败: {e}")
        return

    inp_id = args[2] if len(args) > 2 else None
    neg_id = args[3] if len(args) > 3 else None
    out_id = args[4] if len(args) > 4 else None

    if not ctx.api:
        yield event.plain_result("❌ ComfyUI API 未初始化")
        return

    exists, msg = ctx.api.switch_workflow(
        filename, input_id=inp_id, neg_node_id=neg_id, output_id=out_id
    )

    status = "✅" if exists else "⚠"
    logger.info(f"[ComfyUI] 管理员 {user_id} 切换工作流: {filename}")
    yield event.plain_result(f"{status} {msg}")


async def save_workflow(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """保存/导入工作流"""
    user_id = str(event.get_sender_id())
    if user_id not in ctx.access.admin_ids:
        yield event.plain_result("🚫 权限不足，仅管理员可导入工作流")
        return

    full_text = event.message_str
    content = full_text.split(maxsplit=2)

    if len(content) < 3:
        yield event.plain_result(
            "❌ 参数不足\n"
            "用法：/comfy_save <文件名> <JSON内容>\n"
            '示例：/comfy_save my_workflow.json {"1":{...}}'
        )
        return

    filename = content[1]
    json_str = content[2]

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

        logger.info(f"[ComfyUI] 管理员 {user_id} 导入工作流: {filename}")
        yield event.plain_result(
            f"✅ 保存成功！\n文件：{filename}\n使用 /comfy_ls 查看列表"
        )
    except Exception as e:
        yield event.plain_result(f"❌ 保存失败: {e}")


async def add_steps(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """给当前工作流的指定节点绑定步数覆盖"""
    user_id = str(event.get_sender_id())
    if user_id not in ctx.access.admin_ids:
        yield event.plain_result("🚫 权限不足，仅管理员可设置步数覆盖")
        return

    if not ctx.api:
        yield event.plain_result("❌ ComfyUI API 未初始化")
        return

    args = event.message_str.split()

    if len(args) < 2:
        yield event.plain_result(
            "步数覆盖设置（按节点ID）\n"
            "\n"
            "用法：\n"
            "  /comfy_add <节点ID> <步数>      单个设置\n"
            "  /comfy_add <ID1> <步数1> <ID2> <步数2>  批量设置\n"
            "  /comfy_add <节点ID> off         取消单个\n"
            "  /comfy_add list                 查看当前覆盖\n"
            "  /comfy_add clear                清空所有覆盖\n"
            "\n"
            "示例：\n"
            "  /comfy_add 3839 20              节点3839设为20步\n"
            "  /comfy_add 3839 20 4521 50      同时设置两个节点\n"
            "\n"
            "节点ID可在工作流JSON中查找 ParameterBreak 节点"
        )
        return

    sub_cmd = args[1].lower()

    if sub_cmd == "list":
        async for result in _comfy_add_list(event):
            yield result
        return

    if sub_cmd == "clear":
        async for result in _comfy_add_clear(event):
            yield result
        return

    params = args[1:]

    if len(params) % 2 != 0:
        yield event.plain_result("❌ 参数格式错误，需要成对输入：<节点ID> <步数>")
        return

    current_file = ctx.api.workflow.filename
    stem = Path(current_file).stem
    sidecar_path = ctx.api.workflow.directory / f"{stem}.steps.json"

    existing = {}
    if sidecar_path.exists():
        try:
            with open(sidecar_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    changes = []
    removes = []

    for i in range(0, len(params), 2):
        node_id = params[i]
        value = params[i + 1].lower()

        if value in ("off", "0", "del", "delete", "rm", "remove"):
            if node_id in existing:
                del existing[node_id]
                removes.append(node_id)
        else:
            try:
                steps = int(value)
                if not (1 <= steps <= 200):
                    yield event.plain_result(
                        f"❌ 步数应在 1-200 之间，节点 {node_id} 的值 {value} 无效"
                    )
                    return
                existing[node_id] = {"steps": steps}
                changes.append(f"{node_id}:{steps}步")
            except ValueError:
                yield event.plain_result(f"❌ 无效的步数值：{value}")
                return

    try:
        if existing:
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        else:
            if sidecar_path.exists():
                sidecar_path.unlink()

        msg_parts = []
        if changes:
            msg_parts.append(f"✅ 已设置: {', '.join(changes)}")
        if removes:
            msg_parts.append(f"已移除: {', '.join(removes)}")

        msg_parts.append(f"工作流: {current_file}")

        logger.info(
            f"[ComfyUI] 管理员 {user_id} 修改步数覆盖: {current_file} -> {existing}"
        )
        yield event.plain_result("\n".join(msg_parts))

    except Exception as e:
        yield event.plain_result(f"❌ 保存失败: {e}")


async def _comfy_add_list(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """列出当前工作流的步数覆盖"""
    current_file = ctx.api.workflow.filename
    stem = Path(current_file).stem
    sidecar_path = ctx.api.workflow.directory / f"{stem}.steps.json"

    lines = [
        "当前工作流步数覆盖",
        "",
        f"工作流: {current_file}",
        "",
    ]

    if not sidecar_path.exists():
        lines.append("ℹ暂无步数覆盖配置")
    else:
        try:
            with open(sidecar_path, encoding="utf-8") as f:
                data = json.load(f)

            if not data:
                lines.append("ℹ暂无步数覆盖配置")
            else:
                lines.append("节点覆盖列表：")
                for node_id, value in data.items():
                    if isinstance(value, dict):
                        steps = value.get("steps", "?")
                    else:
                        steps = value
                    lines.append(f"  • 节点 {node_id}: {steps} 步")
        except Exception as e:
            lines.append(f"❌ 读取配置失败: {e}")

    lines.append("")
    lines.append("")
    lines.append("设置：/comfy_add <节点ID> <步数>")
    lines.append("清空：/comfy_add clear")

    yield event.plain_result("\n".join(lines))


async def _comfy_add_clear(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """清空当前工作流的所有步数覆盖"""
    current_file = ctx.api.workflow.filename
    stem = Path(current_file).stem
    sidecar_path = ctx.api.workflow.directory / f"{stem}.steps.json"

    if not sidecar_path.exists():
        yield event.plain_result(f"ℹ{current_file} 本来就没有步数覆盖")
        return

    try:
        sidecar_path.unlink()
        user_id = str(event.get_sender_id())
        logger.info(f"[ComfyUI] 管理员 {user_id} 清空步数覆盖: {current_file}")
        yield event.plain_result(f"✅ 已清空 {current_file} 的所有步数覆盖")
    except Exception as e:
        yield event.plain_result(f"❌ 清空失败: {e}")


async def current_workflow(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """显示当前工作流与三个节点绑定。"""
    workflow = ctx.api.workflow
    lines = [
        "当前 ComfyUI 工作流",
        f"- 文件: {workflow.filename}",
        f"- 正面节点: {workflow.input_node_id}",
        f"- 负面节点: {workflow.neg_node_id or '未绑定'}",
        f"- 输出节点: {workflow.output_node_id or '自动'}",
    ]
    yield event.plain_result("\n".join(lines))


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


def _trigger_preview(
    entry: dict[str, Any],
    limit: int,
    preview_length: int,
) -> str:
    options = list(entry.get("trigger_options", []) or [])[:limit]
    if not options:
        if entry.get("is_style_lora"):
            return "无触发词（更偏全局风格）"
        return "无触发词"

    rendered = []
    for index, option in enumerate(options, start=1):
        text = re.sub(r"\s+", " ", str(option or "").strip()).strip(",; ")
        if len(text) > preview_length:
            text = text[: max(0, preview_length - 1)].rstrip() + "…"
        rendered.append(f"[{index}] {text}")
    return " | ".join(rendered)


async def lora_test(
    ctx: CommandContext,
    event: AstrMessageEvent,
) -> AsyncIterator[MessageEventResult]:
    """查看 LoRA 清单，或预演一次选择解析。"""
    allowed, reason = ctx.access.check_access(event)
    if not allowed:
        yield event.plain_result(reason)
        return

    context = ctx.api.lora.runtime_context()
    if not context.get("supported"):
        yield event.plain_result("⚠当前工作流未检测到 LoRA 堆节点")
        return

    catalog = list(context.get("catalog", {}).values())
    defaults = list(context.get("default_active_loras", []))
    workflow_name = ctx.api.workflow.filename
    scan_roots = context.get("scan_roots", [])
    enabled_text = "开启" if ctx.cfg.llm.lora_control.enabled else "关闭"

    text = (event.message_str or "").strip()
    parts = text.split(None, 1)
    arg_text = parts[1].strip() if len(parts) > 1 else ""
    arg_lower = arg_text.lower()

    if not arg_text or arg_lower in ("help", "status", "状态", "帮助"):
        default_names = (
            ", ".join(item.get("display_name", "") for item in defaults) or "无"
        )
        lines = [
            "LoRA 测试命令",
            f"当前工作流: {workflow_name}",
            f"LoRA 控制功能: {enabled_text}",
            f"已识别 LoRA 数量: {len(catalog)}",
            f"默认启用 LoRA: {default_names}",
        ]
        if scan_roots:
            lines.append("扫描目录:")
            lines.extend(f"- {root}" for root in scan_roots)
        lines.extend(
            [
                "用法示例:",
                "/测试lora 列表",
                "/测试lora 列表 菲比",
                "/测试lora QAQ1121-24:0.8",
                "/测试lora !clear_defaults, 菲比 鸣潮:0.8@1",
                '测试lora <lora picks="!clear_defaults, QAQ1121-24:0.8@1+2">',
                "说明:",
                "- @1 / @1+2 表示选该 LoRA 的第 1 个或第 1+2 个触发词候选",
                "- !clear_defaults 表示本次禁用工作流默认 LoRA",
                "- 如果用了 @序号，系统会自动注入对应触发词",
                "- 这个命令只做解析预演，不会真的出图",
            ]
        )
        yield event.plain_result("\n".join(lines))
        return

    if (
        arg_lower == "列表"
        or arg_lower.startswith("列表 ")
        or arg_lower == "list"
        or arg_lower.startswith("list ")
    ):
        keyword = parts[1].strip() if len(parts) > 1 else ""
        if keyword:
            normalized = normalize_lora_key(keyword)
            matched = []
            for entry in catalog:
                fields = [
                    entry.get("display_name", ""),
                    entry.get("workflow_name", ""),
                    entry.get("loader_name", ""),
                    entry.get("model_name", ""),
                ] + list(entry.get("aliases", []))
                if any(
                    normalized in normalize_lora_key(value) for value in fields if value
                ):
                    matched.append(entry)
        else:
            matched = catalog

        if not matched:
            yield event.plain_result(f"没找到匹配的 LoRA: {keyword}")
            return

        limit = 20
        lines = [f"LoRA 列表 | 工作流: {workflow_name} | 匹配数量: {len(matched)}"]
        if len(matched) > limit:
            lines.append(f"仅展示前 {limit} 个结果")
        for entry in matched[:limit]:
            strength_text = format_strength(entry.get("default_strength", 1.0))
            clip_text = format_strength(
                entry.get("default_clip_strength", entry.get("default_strength", 1.0))
            )
            status_text = "默认启用" if entry.get("default_active") else "可选"
            lines.append(
                f"- {entry.get('display_name')} | {status_text} | "
                f"默认强度 {strength_text}/{clip_text}"
            )
            lines.append(
                "  触发词: "
                + _trigger_preview(
                    entry,
                    ctx.api.lora.settings.max_trigger_options_per_lora,
                    ctx.api.lora.settings.trigger_option_preview_length,
                )
            )
        yield event.plain_result("\n".join(lines))
        return

    parse_text = arg_text
    if arg_lower.startswith("解析 "):
        parse_text = arg_text.split(None, 1)[1].strip()
    elif arg_lower.startswith("test "):
        parse_text = arg_text.split(None, 1)[1].strip()

    if "<lora" in parse_text.lower():
        _, parsed = extract_lora_control_tags(parse_text)
    else:
        parsed = parse_lora_picks(parse_text)

    if not parsed:
        yield event.plain_result(
            "没解析到任何 LoRA 选择\n"
            "示例: /测试lora QAQ1121-24:0.8\n"
            "示例: /测试lora !clear_defaults, 菲比 鸣潮:0.8@1"
        )
        return

    resolved = ctx.api.lora.resolve_selections(parsed)
    preview_prompt, injected_hints = ctx.api.lora.inject_prompt_hints(
        "1girl, masterpiece",
        resolved,
    )
    clear_defaults = any(item.get("control") == "clear_defaults" for item in parsed)
    resolved_names = {
        str(item.get("requested_name", "")).strip()
        for item in resolved
        if item.get("control") != "clear_defaults"
    }
    unresolved = [
        str(item.get("name", "")).strip()
        for item in parsed
        if item.get("control") != "clear_defaults"
        and str(item.get("name", "")).strip()
        and str(item.get("name", "")).strip() not in resolved_names
    ]

    lines = [
        f"LoRA 解析预演 | 工作流: {workflow_name}",
        f"默认 LoRA 处理: {'本次清空' if clear_defaults else '保持默认行为'}",
        f"原始输入: {parse_text}",
        "解析结果:",
    ]
    for item in parsed:
        if item.get("control") == "clear_defaults":
            lines.append("- !clear_defaults")
            continue
        indexes = item.get("trigger_indexes", []) or []
        suffix = f" @ {'+'.join(map(str, indexes))}" if indexes else ""
        lines.append(
            f"- {item.get('name')} | "
            f"强度 {item.get('strength')} / {item.get('clip_strength')}{suffix}"
        )

    lines.append("命中结果:")
    for item in resolved:
        if item.get("control") == "clear_defaults":
            lines.append("- 本次会清空工作流默认 LoRA")
            continue
        strength_text = format_strength(item.get("strength", 1.0))
        clip_text = format_strength(
            item.get("clip_strength", item.get("strength", 1.0))
        )
        indexes = item.get("trigger_indexes", []) or []
        lines.append(
            f"- {item.get('display_name')} | 实际载入名 {item.get('name')} | "
            f"强度 {strength_text}/{clip_text} | "
            f"触发词编号 {'+'.join(map(str, indexes)) or '无'}"
        )
        hints = item.get("selected_prompt_hints", []) or []
        if hints:
            previews = []
            for hint in hints[:3]:
                cleaned = re.sub(r"\s+", " ", str(hint).strip())
                previews.append(cleaned[:80] + ("…" if len(cleaned) > 80 else ""))
            lines.append("  注入提示: " + " | ".join(previews))

    if unresolved:
        lines.append("未命中:")
        lines.extend(f"- {name}" for name in unresolved)

    if injected_hints:
        lines.append("提示词注入预览:")
        lines.append(
            f"- {preview_prompt[:300]}{'…' if len(preview_prompt) > 300 else ''}"
        )

    yield event.plain_result("\n".join(lines))


async def on_llm_request(
    ctx: CommandContext,
    event: AstrMessageEvent,
    req: ProviderRequest,
) -> None:
    """注入系统提示词 + 清理历史中的绘图提示词"""
    try:
        my_prompt = _system_prompt(ctx)

        if my_prompt:
            current_prompt = getattr(req, "system_prompt", "") or ""
            if my_prompt not in current_prompt:
                if current_prompt:
                    req.system_prompt = f"{current_prompt}\n\n{my_prompt}".strip()
                else:
                    req.system_prompt = my_prompt

    except Exception as e:
        logger.error(f"[ComfyUI] 注入提示词异常: {e}")

    try:
        ctx.drawer.failures.inject(event, req)
    except Exception as e:
        logger.error(f"[ComfyUI] 注入绘图失败状态异常: {e}")

    if ctx.cfg.llm.discard_prompt_from_history:
        try:
            _clean_pic_tags_from_req(req)
        except Exception as e:
            logger.error(f"[ComfyUI] 清理提示词异常: {e}")


async def on_llm_response(
    ctx: CommandContext,
    event: AstrMessageEvent,
    resp: LLMResponse,
) -> None:
    """提取 LLM 回复中的提示词（使用 <pic prompt="..."> 格式）"""
    if not resp or not resp.completion_text:
        return

    full_text = resp.completion_text
    cleaned_text = strip_control_tags(full_text, remove_think=True)
    cleaned_entries = extract_usable_prompt_entries(full_text)
    supplemented_entries = await _force_draw_entries(
        ctx,
        event,
        cleaned_text,
        cleaned_entries,
    )
    if supplemented_entries:
        cleaned_entries = cleaned_entries + supplemented_entries
    if not cleaned_entries:
        return

    event.set_extra("comfy_cleaned_text", cleaned_text)

    if not _multi_image_mode(ctx, len(cleaned_entries)):
        event._comfy_extracted_prompt = cleaned_entries[0]["prompt"]
        event._comfy_extracted_loras = cleaned_entries[0].get("lora_selections", [])
        logger.info(f"[ComfyUI] 检测到单图模式: {cleaned_entries[0]['prompt'][:50]}...")
        if ctx.cfg.llm.discard_prompt_from_history:
            resp.completion_text = cleaned_text
            resp.result_chain = MessageChain().message(cleaned_text)
            logger.info("[ComfyUI] 已从历史记录中移除绘图提示词")
        return

    segments = build_segments(full_text, cleaned_entries)
    if segments:
        event._comfy_segments = segments
        logger.info(f"[ComfyUI] 检测到多图模式，共 {len(cleaned_entries)} 张图片")
        if ctx.cfg.llm.discard_prompt_from_history:
            resp.completion_text = cleaned_text
            resp.result_chain = MessageChain().message(cleaned_text)
            logger.info("[ComfyUI] 已从历史记录中移除绘图提示词（多图模式）")


async def stage_build_chain(ctx: CommandContext, event: AstrMessageEvent) -> None:
    """自动绘图 - 阶段1：构建 chain（多图）或启动异步任务（单图）"""
    if getattr(event, "_comfy_auto_painted", False):
        return

    segments = getattr(event, "_comfy_segments", None)
    prompts = [
        item.get("content", "")
        for item in segments or []
        if item.get("type") == "prompt"
    ]

    if segments and _multi_image_mode(ctx, len(prompts)):
        event._comfy_auto_painted = True
        try:
            ctx.drawer.ensure_allowed(event, prompts, "LLM 自动多图")
        except DrawError as exc:
            logger.warning(f"[ComfyUI] 多图请求被拒绝: {exc}")
            await send_text(event, str(exc))
            return

        result = event.get_result()
        if not result:
            return

        new_chain = []
        image_index = 0
        for segment in segments:
            if segment["type"] == "text":
                new_chain.append(Plain(segment["content"]))
            elif segment["type"] == "prompt":
                image_index += 1
                new_chain.append(
                    _ComfyImageMarker(
                        segment["content"],
                        image_index,
                        segment.get("lora_selections", []),
                    )
                )

        result.chain = new_chain
        event.set_extra("comfy_multi_image_mode", True)
        event.set_extra("comfy_multi_prompt_count", image_index)
        logger.info(
            f"[ComfyUI] 多图 chain 已构建: {len(new_chain)} 个元素, "
            f"{image_index} 张图片待生成"
        )
        return

    prompt = getattr(event, "_comfy_extracted_prompt", None)
    if not prompt:
        return

    event._comfy_auto_painted = True
    try:
        ctx.drawer.ensure_allowed(event, [prompt], "LLM 自动绘图")
    except DrawError as exc:
        logger.warning(f"[ComfyUI] 单图请求被拒绝: {exc}")
        await send_text(event, str(exc))
        return

    asyncio.create_task(
        _send_image_async(
            ctx,
            event,
            prompt,
            getattr(event, "_comfy_extracted_loras", []),
        )
    )


async def stage_clean_history(ctx: CommandContext, event: AstrMessageEvent) -> None:
    """在所有处理完成后，直接从对话历史中移除绘图提示词"""
    if not ctx.cfg.llm.discard_prompt_from_history:
        return

    has_prompt = hasattr(event, "_comfy_extracted_prompt") or hasattr(
        event, "_comfy_segments"
    )
    if not has_prompt:
        return

    try:
        conv_mgr = ctx.context.conversation_manager
        unified_msg_origin = event.unified_msg_origin
        conv_id = await conv_mgr.get_curr_conversation_id(unified_msg_origin)

        if not conv_id:
            return

        conversation = await conv_mgr.get_conversation(unified_msg_origin, conv_id)
        if not conversation:
            return

        try:
            history = json.loads(conversation.history) if conversation.history else []
        except json.JSONDecodeError:
            return

        modified = False
        for entry in history:
            if entry.get("role") != "assistant":
                continue
            content = str(entry.get("content", ""))
            cleaned = strip_control_tags(content, remove_think=True)
            if cleaned != content.strip():
                entry["content"] = cleaned
                modified = True

        if modified:
            await conv_mgr.update_conversation(
                unified_msg_origin=unified_msg_origin,
                conversation_id=conv_id,
                history=history,
            )
            logger.info("[ComfyUI] 已从对话历史中清理绘图提示词")

    except Exception as e:
        logger.error(f"[ComfyUI] 清理历史记录失败: {e}")


async def stage_send_images(ctx: CommandContext, event: AstrMessageEvent) -> None:
    """多图模式 - 阶段2：在 HtmlRender 渲染完成后，分组发送"""
    if not event.get_extra("comfy_multi_image_mode"):
        return

    result = event.get_result()
    if not result or not result.chain:
        return

    prompt_count = event.get_extra("comfy_multi_prompt_count") or 0
    logger.info(f"[ComfyUI] 多图发送阶段开始，chain 共 {len(result.chain)} 个元素")

    groups = []
    current_group = []
    for item in result.chain:
        if isinstance(item, _ComfyImageMarker):
            groups.append({"items": current_group, "marker": item})
            current_group = []
        else:
            current_group.append(item)
    if current_group:
        groups.append({"items": current_group, "marker": None})

    ctx.drawer.failures.clear(event)

    for group in groups:
        items = group["items"]
        marker = group["marker"]

        filtered = [
            item
            for item in items
            if not (isinstance(item, Plain) and not item.text.strip())
        ]
        if filtered:
            await send_chain(event, filtered)
            logger.info(f"[ComfyUI] 文字段已发送 ({len(filtered)} 个元素)")

        if not marker:
            continue

        try:
            path = await ctx.drawer.render(
                event,
                marker.prompt,
                marker.lora_selections,
                f"LLM 多图 图片{marker.index}",
                index=marker.index,
                append=True,
            )
        except DrawError as exc:
            await send_text(event, str(exc))
            continue

        await send_chain(event, image_components(path))
        logger.info(
            f"[ComfyUI] [{marker.index}/{prompt_count}] 图片已发送: {path.name}"
        )

    result.chain.clear()
    logger.info("[ComfyUI] 多图模式发送完成")


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


def _system_prompt(ctx: CommandContext) -> str:
    my_prompt = ctx.cfg.llm.system_prompt.strip()
    if not my_prompt:
        return ""

    if ctx.cfg.llm.lora_control.enabled:
        try:
            lora_appendix = ctx.api.lora.prompt_appendix()
        except Exception as e:
            logger.warning(f"[ComfyUI] 获取 LoRA prompt 附录失败: {e}")
            lora_appendix = ""
        if lora_appendix:
            my_prompt = f"{my_prompt}\n\n{lora_appendix}".strip()
    return my_prompt


def _clean_pic_tags_from_req(req: ProviderRequest) -> None:
    """从请求的 conversation.history 中清理 <pic> 标签"""
    conversation = getattr(req, "conversation", None)
    if conversation is None:
        logger.warning("[ComfyUI] req.conversation 不存在，跳过清理")
        return

    history_raw = getattr(conversation, "history", None)
    if not history_raw:
        return

    try:
        history = (
            json.loads(history_raw) if isinstance(history_raw, str) else history_raw
        )
    except (json.JSONDecodeError, TypeError):
        return

    if not isinstance(history, list):
        return

    cleaned = 0
    for entry in history:
        if not isinstance(entry, dict):
            continue
        if entry.get("role") != "assistant":
            continue
        content = entry.get("content", "")
        if isinstance(content, str):
            stripped = strip_control_tags(content, remove_think=True)
            if find_pic_prompts(content) or "<lora " in content.lower():
                entry["content"] = stripped
                cleaned += 1

    if cleaned:
        conversation.history = json.dumps(history, ensure_ascii=False)
        logger.info(
            f"[ComfyUI] 已从 conversation.history 中清理 {cleaned} 条消息的绘图提示词"
        )


def _force_draw_contexts(
    conversation: object,
    latest_reply_text: str,
) -> list[dict]:
    history = coerce_history(
        getattr(conversation, "history", None),
    )
    sanitized_contexts = []

    for entry in history[-FORCE_DRAW_CONTEXT_LIMIT:]:
        message = copy.deepcopy(entry)
        message["content"] = sanitize_context_content(
            message.get("content"),
        )
        content = message.get("content")
        if content in ("", None, []):
            if not message.get("tool_calls") and not message.get("tool_call_id"):
                continue
        sanitized_contexts.append(message)

    latest_reply_text = strip_control_tags(
        latest_reply_text,
        remove_think=True,
    )
    if latest_reply_text:
        should_append = True
        if sanitized_contexts:
            last_message = sanitized_contexts[-1]
            if (
                last_message.get("role") == "assistant"
                and isinstance(last_message.get("content"), str)
                and last_message.get("content", "").strip() == latest_reply_text
            ):
                should_append = False
        if should_append:
            sanitized_contexts.append(
                {"role": "assistant", "content": latest_reply_text},
            )

    return sanitized_contexts[-FORCE_DRAW_CONTEXT_LIMIT:]


async def _force_draw_entries(
    ctx: CommandContext,
    event: AstrMessageEvent,
    latest_reply_text: str,
    current_entries: list[dict] | None = None,
) -> list[dict]:
    if not ctx.cfg.llm.force_draw_when_no_prompt:
        return []

    current_entries = list(current_entries or [])
    current_count = len(current_entries)
    missing_count = max(0, ctx.cfg.llm.target_image_count - current_count)
    needs_fill_when_no_prompt = current_count == 0 and missing_count > 0
    needs_fill_to_target_count = (
        missing_count > 0 and ctx.cfg.llm.target_image_count > 1
    )
    if not needs_fill_when_no_prompt and not needs_fill_to_target_count:
        return []

    latest_reply_text = strip_control_tags(
        latest_reply_text,
        remove_think=True,
    )
    reply_length = count_visible_text_length(latest_reply_text)
    if reply_length < FORCE_DRAW_MIN_REPLY_LENGTH:
        logger.info(
            "[ComfyUI] 跳过强制画图兜底：当前回复长度为 %s，小于阈值 %s",
            reply_length,
            FORCE_DRAW_MIN_REPLY_LENGTH,
        )
        return []

    provider = ctx.context.get_using_provider(event.unified_msg_origin)
    if provider is None:
        logger.warning("[ComfyUI] 强制画图兜底失败：当前没有可用的聊天模型")
        return []

    conversation = None
    try:
        conversation_id = (
            await ctx.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin,
            )
        )
        if conversation_id:
            conversation = await ctx.context.conversation_manager.get_conversation(
                event.unified_msg_origin,
                conversation_id,
            )
    except Exception as e:
        logger.warning(
            f"[ComfyUI] 读取当前会话上下文失败，继续使用空上下文补提示词: {e}"
        )

    contexts = _force_draw_contexts(conversation, latest_reply_text)
    base_system_prompt = _system_prompt(ctx)
    latest_reply_excerpt = re.sub(r"\s+", " ", latest_reply_text).strip()
    if len(latest_reply_excerpt) > 1200:
        latest_reply_excerpt = latest_reply_excerpt[:1199].rstrip() + "…"
    seen_prompts = {
        normalize_prompt_signature(item.get("prompt", ""))
        for item in current_entries
        if item.get("prompt")
    }
    supplemented_entries = []
    max_rounds = 2
    remaining_count = missing_count

    for round_idx in range(max_rounds):
        if remaining_count <= 0:
            break

        existing_entries = current_entries + supplemented_entries
        existing_prompt_summary = ""
        if existing_entries:
            rendered_existing = []
            for item in existing_entries[:8]:
                prompt_text = re.sub(
                    r"\s+", " ", str(item.get("prompt", "") or "")
                ).strip()
                if not prompt_text:
                    continue
                if len(prompt_text) > 180:
                    prompt_text = prompt_text[:179].rstrip() + "…"
                rendered_existing.append(f"- {prompt_text}")
            if rendered_existing:
                existing_prompt_summary = (
                    "\n当前已经有以下图片提示词，补图时不要重复或近似重复这些画面：\n"
                    + "\n".join(rendered_existing)
                )

        if current_count == 0 and len(supplemented_entries) == 0:
            shortage_text = (
                '上一轮回复没有产出可用的 <pic prompt="...">。'
                if ctx.cfg.llm.target_image_count == 1
                else (
                    '上一轮回复没有产出可用的 <pic prompt="...">，'
                    f"目标出图数量为 {ctx.cfg.llm.target_image_count}。"
                )
            )
        else:
            shortage_text = (
                f"当前已经提取到 {current_count + len(supplemented_entries)} 个"
                '可用的 <pic prompt="...">，'
                f"但目标出图数量是 {ctx.cfg.llm.target_image_count}，"
                f"还差 {remaining_count} 个。"
            )

        output_rule = (
            f'1. 只输出 {remaining_count} 个 <pic prompt="...">，'
            "不要输出 <think>、解释、正文、代码块或 Markdown；"
        )
        count_rule = (
            f"3. 必须补足 {remaining_count} 张图，"
            '每个 <pic prompt="..."> 对应一张不同的图，不要少于或多于该数量；'
        )
        fallback_prompt = (
            "下面是最近的一条回复内容：\n"
            f"{latest_reply_excerpt}\n\n"
            f"请只基于上面这条最近回复内容，补齐剩余 {remaining_count} 张图。"
            '只返回对应数量的 <pic prompt="..."> 标签，可按换行分隔。'
            "如果有满足动作（如壁屄）或人物要求的lora，则直接使用lora，注意人物一致性"
        )

        force_draw_instruction = (
            "你现在处于补图提示词模式。"
            f"{shortage_text}"
            f"{existing_prompt_summary}\n"
            "硬性要求：\n"
            f"{output_rule}\n"
            "2. prompt 必须是适合 ComfyUI / Stable Diffusion / Danbooru 的英文 tags，"
            "半角逗号分隔；\n"
            f"{count_rule}\n"
            "4. 只能基于最近的一条回复内容来补图，不要参考更早的整体会话上下文；\n"
            "5. 如果信息不足，就提炼最近一条回复里最值得定格的不同瞬间；\n"
            "6. 不要输出占位符，不要留空，不要重复已有画面。"
        )
        system_prompt = (
            f"{base_system_prompt}\n\n{force_draw_instruction}".strip()
            if base_system_prompt
            else force_draw_instruction
        )

        try:
            response = await provider.text_chat(
                prompt=fallback_prompt,
                contexts=contexts,
                system_prompt=system_prompt,
            )
        except Exception as e:
            logger.error(f"[ComfyUI] 强制画图补提示词请求失败: {e}")
            logger.error(traceback.format_exc())
            break

        fallback_text = str(getattr(response, "completion_text", "") or "").strip()
        if not fallback_text:
            logger.warning("[ComfyUI] 强制画图补提示词返回空内容")
            break

        round_entries = extract_usable_prompt_entries(fallback_text)
        if not round_entries:
            logger.warning(
                "[ComfyUI] 强制画图补提示词后仍未提取到可用 prompt: %s",
                fallback_text[:160],
            )
            break

        added_this_round = 0
        for entry in round_entries:
            prompt_signature = normalize_prompt_signature(entry.get("prompt", ""))
            if not prompt_signature or prompt_signature in seen_prompts:
                continue
            supplemented_entries.append(entry)
            seen_prompts.add(prompt_signature)
            added_this_round += 1
            if len(supplemented_entries) >= missing_count:
                break

        if added_this_round <= 0:
            logger.warning(
                "[ComfyUI] 强制画图补提示词返回的内容与已有 prompt 重复，停止补图"
            )
            break

        remaining_count = max(0, missing_count - len(supplemented_entries))

    if supplemented_entries:
        logger.info(
            "[ComfyUI] 强制画图补提示词成功，共补出 %s 张图",
            len(supplemented_entries),
        )
    return supplemented_entries


def _multi_image_mode(ctx: CommandContext, prompt_count: int) -> bool:
    return prompt_count > 1 and (
        ctx.cfg.llm.multi_image_mode or ctx.cfg.llm.target_image_count > 1
    )


async def _send_image_async(
    ctx: CommandContext,
    event: AstrMessageEvent,
    prompt: str,
    lora_selections=None,
) -> None:
    """异步生成并发送图片（不阻塞文字消息发送）"""
    try:
        path = await ctx.drawer.draw(event, prompt, lora_selections, "LLM 自动绘图")
    except DrawError as exc:
        await send_text(event, str(exc))
        return

    await send_chain(event, image_components(path))
    logger.info(f"[ComfyUI] 异步图片已发送: {path.name}")
