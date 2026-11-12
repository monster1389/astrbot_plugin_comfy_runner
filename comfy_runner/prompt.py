"""提示词标签的解析与清理。"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, NotRequired, TypedDict

from astrbot.api import logger

PIC_PROMPT_PATTERN = re.compile(
    r'<pic\b[^>]*\bprompt=(?P<quote>["\'])(?P<prompt>.*?)(?P=quote)[^>]*?/?>\s*(?:</pic>)?',
    flags=re.DOTALL | re.IGNORECASE,
)
PIC_PROMPT_UNCLOSED_PATTERN = re.compile(
    r'<pic\b(?P<prefix>[^>]*\bprompt=)(?P<quote>["\'])(?P<prompt>.*?)</pic>',
    flags=re.DOTALL | re.IGNORECASE,
)
LORA_PICKS_PATTERN = re.compile(
    r'<lora\s+picks="(.*?)"\s*/?>',
    flags=re.DOTALL | re.IGNORECASE,
)
RENDER_OPEN_PATTERN = re.compile(r"<render\b[^>]*>")
PLACEHOLDER_PATTERNS = (
    r"^\.{2,}$",
    r"^…+$",
    r"^[.。]+$",
    r"^[xX]{2,}$",
    r"^[-_=]{2,}$",
    r"^\[.*?\]$",
    r"^\{.*?\}$",
)
PROMPT_PREVIEW_LIMIT = 240
LORA_CLEAR_TOKEN = "!clear_defaults"


class PicPrompt(TypedDict):
    """文本里命中的一处图片标签。"""

    start: int
    end: int
    prompt: str


class LoraPick(TypedDict, total=False):
    """文本里解析出的一条 LoRA 选择。"""

    name: str
    strength: float | None
    clip_strength: float | None
    trigger_indexes: list[int]
    control: str


class PromptEntry(TypedDict):
    """一条可用的绘图提示词。"""

    prompt: str
    lora_selections: list[LoraPick]


class Segment(TypedDict):
    """多图模式下的一段文字或一张图。"""

    type: str
    content: str
    lora_selections: NotRequired[list[LoraPick]]


def normalize_lora_key(value: object) -> str:
    """把 LoRA 名称归一化成用于比较的键。

    Args:
        value: 原始名称，允许为 None。

    Returns:
        去掉空白并转小写后的名称，None 返回空串。
    """
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value)).casefold()


def parse_lora_picks(raw_text: str) -> list[LoraPick]:
    """解析 ``<lora picks="...">`` 标签里的选择串。

    Args:
        raw_text: 标签内部原文，形如 ``name:1.2:0.8@1|2``，逗号分隔多条。

    Returns:
        去重后的 LoRA 选择列表，``!clear_defaults`` 以 control 字段表示。
    """
    if not raw_text:
        return []

    selections: list[LoraPick] = []
    clear_token = normalize_lora_key(LORA_CLEAR_TOKEN)
    for piece in re.split(r"[,;\n，；]+", raw_text):
        token = piece.strip().strip("`").strip()
        if not token:
            continue

        if normalize_lora_key(token) == clear_token:
            selections.append({"name": LORA_CLEAR_TOKEN, "control": "clear_defaults"})
            continue

        trigger_indexes: list[int] = []
        if "@" in token:
            token, trigger_part = token.rsplit("@", 1)
            for raw_index in re.split(r"[+|/、\s]+", trigger_part.strip()):
                raw_index = raw_index.strip()
                if not raw_index:
                    continue
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                if index > 0:
                    trigger_indexes.append(index)

        parts = [part.strip() for part in token.rsplit(":", 2)]
        name = ""
        strength = None
        clip_strength = None

        if len(parts) == 1:
            name = parts[0]
        elif len(parts) == 2:
            name, strength = parts
        else:
            name, strength, clip_strength = parts

        if not name:
            continue

        try:
            if strength not in ("", None):
                strength = float(strength)
            else:
                strength = None
        except (TypeError, ValueError):
            strength = None

        try:
            if clip_strength not in ("", None):
                clip_strength = float(clip_strength)
            else:
                clip_strength = None
        except (TypeError, ValueError):
            clip_strength = None

        selections.append(
            {
                "name": name,
                "strength": strength,
                "clip_strength": clip_strength,
                "trigger_indexes": trigger_indexes,
            }
        )

    deduped: list[LoraPick] = []
    seen: set[str] = set()
    for item in selections:
        key = normalize_lora_key(item.get("name") or item.get("control"))
        if not key or key in seen:
            continue
        deduped.append(item)
        seen.add(key)
    return deduped


def extract_lora_control_tags(text: str) -> tuple[str, list[LoraPick]]:
    """摘掉文本里的 LoRA 标签并收集其中的选择。

    Args:
        text: 原始文本。

    Returns:
        去掉 LoRA 标签并压缩空白后的文本，以及按出现顺序收集的选择。
    """
    if not isinstance(text, str) or not text:
        return text, []

    collected: list[LoraPick] = []

    def repl(match: re.Match[str]) -> str:
        collected.extend(parse_lora_picks(match.group(1)))
        return ""

    cleaned = LORA_PICKS_PATTERN.sub(repl, text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, collected


def find_pic_prompts(text: str) -> list[PicPrompt]:
    """找出文本里所有图片标签的位置与提示词。

    Args:
        text: 原始文本。

    Returns:
        按起始位置排序的命中列表；未闭合的 ``</pic>`` 写法也会命中。
    """
    if not isinstance(text, str) or not text:
        return []

    matches: list[PicPrompt] = []
    occupied: list[tuple[int, int]] = []

    for match in PIC_PROMPT_PATTERN.finditer(text):
        start, end = match.span()
        matches.append(
            {
                "start": start,
                "end": end,
                "prompt": match.group("prompt") or "",
            }
        )
        occupied.append((start, end))

    for match in PIC_PROMPT_UNCLOSED_PATTERN.finditer(text):
        start, end = match.span()
        if any(not (end <= s or start >= e) for s, e in occupied):
            continue
        matches.append(
            {
                "start": start,
                "end": end,
                "prompt": match.group("prompt") or "",
            }
        )

    matches.sort(key=lambda item: item["start"])
    return matches


def remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """删除文本中若干不重叠的区间。

    Args:
        text: 原始文本。
        spans: 按起点升序排列的区间列表。

    Returns:
        删除后的文本。
    """
    if not isinstance(text, str) or not spans:
        return text

    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def strip_control_tags(text: str, remove_think: bool = False) -> str:
    """删除绘图控制标签，留下可以展示给用户的文字。

    Args:
        text: 原始文本。
        remove_think: 是否一并删除 ``<think>`` 段。

    Returns:
        清理后的文本。
    """
    if not isinstance(text, str):
        return text

    cleaned = LORA_PICKS_PATTERN.sub("", text)
    pic_matches = find_pic_prompts(cleaned)
    spans = [(item["start"], item["end"]) for item in pic_matches]
    cleaned = remove_spans(cleaned, spans)
    cleaned = re.sub(r"</pic>", "", cleaned, flags=re.IGNORECASE)
    if remove_think:
        cleaned = re.sub(
            r"<think>.*?</think>",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
    cleaned = re.sub(r"</?ctx>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def count_visible_text_length(text: object) -> int:
    """统计去掉空白后的可见文字长度。

    Args:
        text: 待统计的文本。

    Returns:
        可见字符数，非字符串返回 0。
    """
    if not isinstance(text, str):
        return 0
    return len(re.sub(r"\s+", "", text))


def normalize_prompt_signature(prompt: object) -> str:
    """把提示词归一化成用于判重的签名。

    Args:
        prompt: 原始提示词。

    Returns:
        压缩空白并转小写后的提示词，非字符串返回空串。
    """
    if not isinstance(prompt, str):
        return ""
    return re.sub(r"\s+", " ", prompt).strip().casefold()


def compact_prompt(prompt: object, limit: int = PROMPT_PREVIEW_LIMIT) -> str:
    """把提示词压成一行并截断。

    Args:
        prompt: 提示词，允许是列表。
        limit: 保留的最大字符数。

    Returns:
        单行提示词，超长时以省略号结尾。
    """
    if prompt is None:
        return ""
    if isinstance(prompt, (list, tuple)):
        prompt = " | ".join(str(item) for item in prompt if item is not None)
    text = re.sub(r"\s+", " ", str(prompt)).strip()
    if not text:
        return ""
    return text[:limit] + ("..." if len(text) > limit else "")


def extract_prompt_lora_pairs(full_text: str) -> list[PromptEntry]:
    """按出现顺序把 LoRA 标签绑定到其后的图片标签。

    Args:
        full_text: LLM 回复原文。

    Returns:
        每条提示词带上它前面累积的 LoRA 选择。
    """
    tokens: list[dict[str, Any]] = []
    for match in LORA_PICKS_PATTERN.finditer(full_text or ""):
        tokens.append(
            {
                "type": "lora",
                "start": match.start(),
                "lora_raw": match.group(1) or "",
            }
        )
    for match in find_pic_prompts(full_text or ""):
        tokens.append(
            {
                "type": "pic",
                "start": match["start"],
                "prompt_raw": match["prompt"],
            }
        )
    tokens.sort(key=lambda item: item["start"])

    pairs: list[PromptEntry] = []
    pending_loras: list[LoraPick] = []
    for token in tokens:
        if token["type"] == "lora":
            pending_loras.extend(parse_lora_picks(token["lora_raw"]))
            continue

        pairs.append(
            {
                "prompt": token["prompt_raw"] or "",
                "lora_selections": pending_loras,
            }
        )
        pending_loras = []

    return pairs


def extract_usable_prompt_entries(full_text: str) -> list[PromptEntry]:
    """从 LLM 回复里提取可以真正送去绘图的提示词。

    Args:
        full_text: LLM 回复原文。

    Returns:
        过滤掉占位符、过短内容与重复 LoRA 后的提示词列表。
    """
    prompt_entries = extract_prompt_lora_pairs(full_text)
    if not prompt_entries:
        return []

    cleaned_entries: list[PromptEntry] = []
    for entry in prompt_entries:
        prompt_text = entry.get("prompt", "")
        prompt_text = re.sub(r"^提示词是\s*[:：]?\s*", "", prompt_text).strip()
        prompt_text = prompt_text.strip('`"\'""''').strip()
        prompt_text, inline_loras = extract_lora_control_tags(prompt_text)
        if not prompt_text:
            continue
        if len(prompt_text) < 3:
            logger.debug(f"[ComfyUI] 跳过过短提示词: '{prompt_text}'")
            continue
        if any(re.match(pattern, prompt_text) for pattern in PLACEHOLDER_PATTERNS):
            logger.debug(f"[ComfyUI] 跳过占位符提示词: '{prompt_text}'")
            continue

        lora_selections: list[LoraPick] = []
        seen_loras: set[str] = set()
        for item in (entry.get("lora_selections") or []) + inline_loras:
            key = normalize_lora_key(item.get("name"))
            if not key or key in seen_loras:
                continue
            lora_selections.append(item)
            seen_loras.add(key)

        cleaned_entries.append(
            {
                "prompt": prompt_text,
                "lora_selections": lora_selections,
            }
        )

    return cleaned_entries


def sanitize_context_content(content: Any) -> Any:
    """清掉上下文内容里的绘图标签，供二次请求 LLM 使用。

    Args:
        content: 历史消息的 content，可以是字符串、列表或其它。

    Returns:
        同结构的新对象，字符串与文本块里的标签已被清理。
    """
    if isinstance(content, str):
        return strip_control_tags(content, remove_think=True)

    if not isinstance(content, list):
        return content

    sanitized_parts: list[Any] = []
    for part in content:
        if isinstance(part, str):
            cleaned_part = strip_control_tags(part, remove_think=True)
            if cleaned_part:
                sanitized_parts.append(cleaned_part)
            continue

        if not isinstance(part, dict):
            sanitized_parts.append(copy.deepcopy(part))
            continue

        item = copy.deepcopy(part)
        item_type = str(item.get("type", "") or "").strip().lower()
        if item_type == "think":
            continue

        if item_type == "text":
            if "text" in item:
                item["text"] = strip_control_tags(
                    str(item.get("text", "") or ""),
                    remove_think=True,
                )
                if not item["text"]:
                    continue
            elif isinstance(item.get("data"), dict):
                item["data"] = copy.deepcopy(item["data"])
                item["data"]["text"] = strip_control_tags(
                    str(item["data"].get("text", "") or ""),
                    remove_think=True,
                )
                if not item["data"]["text"]:
                    continue

        sanitized_parts.append(item)

    return sanitized_parts


def coerce_history(raw_history: Any) -> list[dict[str, Any]]:
    """把会话历史统一成消息字典列表。

    Args:
        raw_history: JSON 字符串或列表。

    Returns:
        深拷贝后的消息列表，无法识别时返回空列表。
    """
    if raw_history is None:
        return []

    if isinstance(raw_history, str):
        try:
            raw_history = json.loads(raw_history)
        except (json.JSONDecodeError, TypeError):
            return []

    if not isinstance(raw_history, list):
        return []

    return [copy.deepcopy(item) for item in raw_history if isinstance(item, dict)]


def build_segments(full_text: str, entries: list[PromptEntry]) -> list[Segment]:
    """把多图回复切成文字段与图片段交替的序列。

    Args:
        full_text: LLM 回复原文。
        entries: 已提取的可用提示词。

    Returns:
        文字段与图片段交替的序列，文字段保留 ``<render>`` 标签对。
    """
    prompts = [entry["prompt"] for entry in entries]
    parts: list[str] = []
    cursor = 0
    for item in find_pic_prompts(full_text):
        parts.append(full_text[cursor : item["start"]])
        cursor = item["end"]
    parts.append(full_text[cursor:])

    render_match = RENDER_OPEN_PATTERN.search(full_text)
    render_open_tag = render_match.group(0) if render_match else ""

    segments: list[Segment] = []
    prompt_index = 0
    for text in parts:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"</?ctx>", "", text)
        text = re.sub(r"</pic>", "", text, flags=re.IGNORECASE)
        text = LORA_PICKS_PATTERN.sub("", text)
        text = text.strip()
        if text:
            if render_open_tag:
                has_open = bool(re.search(r"<render\b", text))
                has_close = "</render>" in text
                if has_open and not has_close:
                    text = text + "</render>"
                elif has_close and not has_open:
                    text = render_open_tag + text
                elif not has_open and not has_close:
                    text = render_open_tag + text + "</render>"
            segments.append({"type": "text", "content": text})
        if prompt_index < len(prompts):
            segments.append(
                {
                    "type": "prompt",
                    "content": prompts[prompt_index],
                    "lora_selections": entries[prompt_index].get("lora_selections", []),
                }
            )
            prompt_index += 1

    while prompt_index < len(prompts):
        segments.append(
            {
                "type": "prompt",
                "content": prompts[prompt_index],
                "lora_selections": entries[prompt_index].get("lora_selections", []),
            }
        )
        prompt_index += 1

    return segments
