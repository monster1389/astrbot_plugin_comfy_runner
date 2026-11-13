"""工作流文件的读写与工作流图的参数注入。"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .config import NodeBinding

LORA_MANIFEST_SUFFIX = ".lora.json"
STEPS_OVERRIDE_SUFFIX = ".steps.json"
POSITIVE_INPUT_KEYS = (
    "text",
    "opt_text",
    "string",
    "text_positive",
    "positive",
    "prompt",
    "wildcard_text",
)
STEPS_INPUT_KEYS = ("steps", "steps_total")
STEPS_OVERRIDE_CLASS = "ParameterBreak"
LORA_CLEAR_TOKEN = "!clear_defaults"


def is_aux_file(filename: str) -> bool:
    """判断是否是工作流的附属文件。

    Args:
        filename: 文件名。

    Returns:
        步数覆盖或 LoRA 清单文件返回 True。
    """
    return filename.endswith(STEPS_OVERRIDE_SUFFIX) or filename.endswith(
        LORA_MANIFEST_SUFFIX
    )


def normalize_lora_key(value: object) -> str:
    """把 LoRA 名称归一化成用于比较的键。

    Args:
        value: 原始名称，允许为 None。

    Returns:
        去掉空白、统一斜杠并转小写后的名称。
    """
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value).replace("\\", "/")).casefold()


def coerce_strength(value: object, default: float = 1.0) -> float:
    """把配置或工作流里的强度转成浮点数。

    Args:
        value: 原始值。
        default: 空值或无法解析时的回退值。

    Returns:
        强度浮点数。
    """
    if value in ("", None):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def format_strength(value: object) -> str:
    """把强度格式化成工作流里的字符串写法。

    Args:
        value: 强度值。

    Returns:
        去掉多余零的十进制字符串。
    """
    text = f"{round(float(value), 4):.4f}".rstrip("0").rstrip(".")
    return text or "0"


class Workflow:
    """当前工作流的文件位置与节点绑定。"""

    def __init__(
        self,
        directory: Path,
        filename: str,
        bindings: Mapping[str, NodeBinding],
    ) -> None:
        """记录工作流目录、文件名与该文件名对应的节点绑定。

        Args:
            directory: 工作流文件目录。
            filename: 当前工作流文件名。
            bindings: 工作流文件名到节点绑定的映射。
        """
        self.directory = directory
        self.filename = filename
        self.bindings = bindings
        self.registered = False
        self.input_node_id = ""
        self.neg_node_id = ""
        self.output_node_id = ""
        self._apply_binding(filename)

    def _apply_binding(self, filename: str) -> None:
        """把指定文件名的绑定写成当前节点 ID，未登记时清空。"""
        binding = self.bindings.get(filename)
        self.registered = binding is not None
        self.input_node_id = binding.input_node_id if binding else ""
        self.neg_node_id = binding.neg_node_id if binding else ""
        self.output_node_id = binding.output_node_id if binding else ""

    @property
    def path(self) -> Path:
        """当前工作流文件路径。"""
        return self.directory / self.filename

    @property
    def manifest_path(self) -> Path:
        """当前工作流的 LoRA 清单路径。"""
        return self.directory / f"{self.path.stem}{LORA_MANIFEST_SUFFIX}"

    @property
    def steps_override_path(self) -> Path:
        """当前工作流的步数覆盖文件路径。"""
        return self.directory / f"{self.path.stem}{STEPS_OVERRIDE_SUFFIX}"

    def list_files(self) -> list[str]:
        """列出目录里可用的工作流文件。

        Returns:
            排除附属文件后的文件名，按字典序排列。
        """
        return sorted(
            entry.name
            for entry in self.directory.glob("*.json")
            if not is_aux_file(entry.name)
        )

    def switch(self, filename: str) -> bool:
        """切换当前工作流并应用该文件名的绑定。

        Args:
            filename: 新的工作流文件名。

        Returns:
            新工作流文件是否存在。
        """
        self.filename = filename
        self._apply_binding(filename)
        return self.path.exists()

    def load(self) -> dict[str, Any]:
        """读取当前工作流。

        Returns:
            工作流图。

        Raises:
            FileNotFoundError: 工作流文件不存在或未配置。
        """
        if not self.filename:
            raise FileNotFoundError("未配置工作流文件")
        if not self.path.exists():
            raise FileNotFoundError(f"工作流文件不存在: {self.path}")
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)


def inject_prompt(graph: dict[str, Any], node_id: str, prompt: str) -> None:
    """把正面提示词写进指定节点。

    Args:
        graph: 工作流图。
        node_id: 正面提示词节点 ID。
        prompt: 提示词文本。
    """
    node = graph.get(node_id)
    if not node:
        logger.error(f"critical: input node id {node_id} not found in workflow")
        return

    inputs = node.get("inputs", {})
    for key in POSITIVE_INPUT_KEYS:
        if key in inputs:
            inputs[key] = prompt
            break


def inject_seeds(graph: dict[str, Any]) -> int:
    """给工作流里所有 seed / noise_seed 填入同一组递增值。

    Args:
        graph: 工作流图。

    Returns:
        改写过的节点数量。
    """
    base_seed = random.randint(1, 999999999999999)
    offset = 0
    changed_nodes = 0

    for node_data in graph.values():
        if not isinstance(node_data, dict):
            continue
        inputs = node_data.get("inputs", {})
        if not isinstance(inputs, dict):
            continue

        changed = False
        if "seed" in inputs:
            inputs["seed"] = base_seed + offset
            offset += 1
            changed = True
        if "noise_seed" in inputs:
            inputs["noise_seed"] = base_seed + offset
            offset += 1
            changed = True

        if changed:
            changed_nodes += 1

    logger.info(
        f"[ComfyUI] base seed: {base_seed}, "
        f"updated {changed_nodes} seed/noise_seed inputs"
    )
    return changed_nodes


def load_steps_override(path: Path) -> dict[str, int]:
    """读取步数覆盖文件。

    Args:
        path: 覆盖文件路径。

    Returns:
        节点 ID 到步数的映射，读取失败时记录警告并返回空映射。
    """
    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"[ComfyUI] failed to read steps override file: {exc}")
        return {}

    if not isinstance(data, dict):
        return {}

    result: dict[str, int] = {}
    for key, value in data.items():
        if isinstance(value, dict) and "steps" in value:
            steps = value.get("steps")
            if isinstance(steps, (int, float)) and steps > 0:
                result[str(key)] = int(steps)
        elif isinstance(value, (int, float)) and value > 0:
            result[str(key)] = int(value)
    return result


def apply_steps_override(graph: dict[str, Any], overrides: dict[str, int]) -> int:
    """按节点 ID 改写引用该节点的 steps 输入。

    Args:
        graph: 工作流图。
        overrides: 节点 ID 到步数的映射。

    Returns:
        改写过的输入数量。
    """
    if not overrides:
        return 0

    override_nodes: set[str] = set()
    for node_id, node_data in graph.items():
        if not isinstance(node_data, dict):
            continue
        if node_data.get("class_type") == STEPS_OVERRIDE_CLASS:
            override_nodes.add(str(node_id))
    if not override_nodes:
        logger.debug("[ComfyUI] no ParameterBreak nodes detected")
        return 0

    valid = {
        node_id: steps
        for node_id, steps in overrides.items()
        if node_id in override_nodes
    }
    for node_id in overrides:
        if node_id not in override_nodes:
            logger.warning(
                f"[ComfyUI] override target node {node_id} "
                "not found in current workflow"
            )
    if not valid:
        return 0

    changed = 0
    for node_id, node_data in graph.items():
        if not isinstance(node_data, dict):
            continue
        inputs = node_data.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        for key in STEPS_INPUT_KEYS:
            value = inputs.get(key)
            if not isinstance(value, list) or len(value) != 2:
                continue
            reference = str(value[0])
            if reference in valid:
                inputs[key] = valid[reference]
                changed += 1
                logger.debug(
                    f"[ComfyUI] node {node_id}.{key}: "
                    f"[{reference}] -> {valid[reference]}"
                )
    return changed


def find_lora_loader_nodes(graph: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """找出承载 LoRA 堆的节点。

    Args:
        graph: 工作流图。

    Returns:
        (节点 ID, 节点数据) 列表，仅包含带非空 LoRA 条目的节点。
    """
    nodes: list[tuple[str, dict[str, Any]]] = []
    for node_id, node_data in graph.items():
        if not isinstance(node_data, dict):
            continue
        inputs = node_data.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        loras = inputs.get("loras")
        if not isinstance(loras, dict):
            continue
        entries = loras.get("__value__")
        if not isinstance(entries, list):
            continue
        if not any(
            isinstance(item, dict) and str(item.get("name", "")).strip()
            for item in entries
        ):
            continue
        nodes.append((str(node_id), node_data))
    return nodes


def rebuild_lora_text(items: list[Any], active_only: bool = True) -> str:
    """把 LoRA 条目拼回节点里的文本写法。

    Args:
        items: 节点里的 LoRA 条目列表。
        active_only: 是否只输出激活的条目。

    Returns:
        以逗号分隔的 ``<lora:name:strength>`` 串。
    """
    tokens: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if active_only and not item.get("active", False):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue

        strength = format_strength(coerce_strength(item.get("strength"), 1.0))
        clip_strength = format_strength(
            coerce_strength(item.get("clipStrength"), item.get("strength") or 1.0)
        )
        if strength == clip_strength:
            tokens.append(f"<lora:{name}:{strength}>")
        else:
            tokens.append(f"<lora:{name}:{strength}:{clip_strength}>")
    return ", ".join(tokens)


def apply_lora_selections(
    graph: dict[str, Any],
    selections: list[dict[str, Any]],
    keep_defaults: bool,
) -> list[str]:
    """把选中的 LoRA 写进工作流的 LoRA 堆节点。

    Args:
        graph: 工作流图。
        selections: 已解析的 LoRA 选择。
        keep_defaults: 未选中的默认 LoRA 是否保持激活。

    Returns:
        实际生效的 LoRA 显示名，按首次出现顺序去重。
    """
    if not selections:
        return []

    loader_nodes = find_lora_loader_nodes(graph)
    if not loader_nodes:
        return []

    clear_defaults = any(item.get("control") == "clear_defaults" for item in selections)
    effective = [item for item in selections if not item.get("control")]
    selected_lookup: dict[str, dict[str, Any]] = {}
    for selection in effective:
        for candidate in (
            selection.get("name"),
            selection.get("workflow_name"),
            selection.get("loader_name"),
            selection.get("display_name"),
            selection.get("requested_name"),
        ):
            key = normalize_lora_key(candidate)
            if key:
                selected_lookup.setdefault(key, selection)

    applied: list[str] = []

    for _, node_data in loader_nodes:
        inputs = node_data.get("inputs", {})
        items = inputs.get("loras", {}).get("__value__", [])
        if not isinstance(items, list):
            continue

        node_applied_keys: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue

            selection = selected_lookup.get(normalize_lora_key(name))
            if selection:
                item["active"] = True
                item["strength"] = format_strength(selection["strength"])
                item["clipStrength"] = format_strength(selection["clip_strength"])
                node_applied_keys.add(normalize_lora_key(selection.get("name")))
                label = selection.get("display_name") or name
                if label not in applied:
                    applied.append(label)
            elif clear_defaults or not keep_defaults:
                item["active"] = False

        for selection in effective:
            selection_key = normalize_lora_key(selection.get("name"))
            if selection_key in node_applied_keys:
                continue
            runtime_name = (
                selection.get("loader_name")
                or selection.get("workflow_name")
                or selection.get("name")
            )
            if not runtime_name:
                continue
            items.append(
                {
                    "name": runtime_name,
                    "strength": format_strength(selection["strength"]),
                    "active": True,
                    "expanded": False,
                    "clipStrength": format_strength(selection["clip_strength"]),
                }
            )
            node_applied_keys.add(selection_key)
            label = selection.get("display_name") or runtime_name
            if label not in applied:
                applied.append(label)

        inputs["text"] = rebuild_lora_text(items, active_only=True)

    return applied
