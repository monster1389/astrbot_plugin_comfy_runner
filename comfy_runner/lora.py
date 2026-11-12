"""LoRA 目录扫描、清单合并与选择解析。"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .config import LoraControlSettings
from .workflow import (
    Workflow,
    coerce_strength,
    find_lora_loader_nodes,
    format_strength,
    normalize_lora_key,
)

LORA_CONTEXT_CACHE_SECONDS = 60
LORA_FILE_EXTENSIONS = {
    ".safetensors",
    ".ckpt",
    ".pt",
    ".pth",
    ".bin",
    ".pt2",
    ".pkl",
    ".sft",
}
LORA_CLEAR_TOKEN = "!clear_defaults"


class LoraManager:
    """当前工作流可用的 LoRA 清单与选择解析。"""

    def __init__(self, settings: LoraControlSettings, workflow: Workflow) -> None:
        """初始化 LoRA 管理器。

        Args:
            settings: LoRA 控制配置。
            workflow: 当前工作流。
        """
        self.settings = settings
        self.workflow = workflow
        self.scan_roots = self._parse_lora_scan_roots(settings.scan_directories)
        self._lora_context_cache = None
        self._lora_context_cache_key = None
        self._lora_context_cache_at = 0.0
        self._lora_scan_cache = None
        self._lora_scan_cache_key = None
        self._lora_scan_cache_at = 0.0

    @staticmethod
    def _parse_lora_scan_roots(value) -> list:
        if isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = re.split(r"[\r\n]+", str(value or ""))

        roots = []
        seen = set()
        for raw in raw_items:
            path_text = str(raw or "").strip().strip('"').strip("'")
            if not path_text:
                continue
            path = Path(path_text).expanduser()
            try:
                path = path.resolve()
            except Exception:
                path = Path(os.path.abspath(str(path)))
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            roots.append(path)
        return roots

    def clear_caches(self) -> None:
        """清空 LoRA 扫描与上下文缓存。"""
        self._lora_context_cache = None
        self._lora_context_cache_key = None
        self._lora_context_cache_at = 0.0
        self._lora_scan_cache = None
        self._lora_scan_cache_key = None
        self._lora_scan_cache_at = 0.0

    @staticmethod
    def _normalize_lora_key(value) -> str:
        if value is None:
            return ""
        return re.sub(r"\s+", "", str(value).replace("\\", "/")).casefold()

    @staticmethod
    def _coerce_lora_strength(value, default: float = 1.0) -> float:
        try:
            if value in ("", None):
                return float(default)
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _format_lora_strength(value: float) -> str:
        number = round(float(value), 4)
        text = f"{number:.4f}".rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _normalize_prompt_token(value) -> str:
        if value is None:
            return ""
        return re.sub(r"\s+", " ", str(value).strip()).casefold()

    @staticmethod
    def _read_json_file(path: Path):
        for encoding in ("utf-8", "utf-8-sig"):
            try:
                with open(path, encoding=encoding) as f:
                    return json.load(f)
            except UnicodeError:
                continue
        with open(path, encoding="utf-8", errors="ignore") as f:
            return json.load(f)

    @staticmethod
    def _trim_preview_text(value: str, max_length: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip()).strip(",; ")
        if len(text) <= max_length:
            return text
        return text[: max(0, max_length - 1)].rstrip() + "…"

    @staticmethod
    def _extract_quoted_segments(value: str) -> list:
        text = str(value or "")
        segments = []
        for pattern in (r"“([^”]+)”", r'"([^"]+)"', r"'([^']+)'"):
            segments.extend(
                match.strip() for match in re.findall(pattern, text) if match.strip()
            )
        return segments

    def _split_trigger_options(self, value) -> list:
        text = re.sub(r"\s+", " ", str(value or "").strip()).strip(",; ")
        if not text:
            return []

        quoted_segments = self._extract_quoted_segments(text)
        if quoted_segments:
            return quoted_segments
        return [text]

    def _expand_prompt_hint_tokens(self, values) -> list:
        expanded = []
        seen = set()
        for raw in values or []:
            pieces = re.split(r"[,;\n，；]+", str(raw or ""))
            if len(pieces) <= 1:
                pieces = [str(raw or "")]

            for piece in pieces:
                text = re.sub(r"\s+", " ", str(piece or "").strip()).strip(",; ")
                normalized = self._normalize_prompt_token(text)
                if not text or not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                expanded.append(text)
        return expanded

    def _iter_lora_lookup_keys(self, entry: dict):
        raw_values = [
            entry.get("display_name"),
            entry.get("name"),
            entry.get("workflow_name"),
            entry.get("loader_name"),
            entry.get("model_name"),
        ] + list(entry.get("aliases", []))

        seen = set()
        for raw in raw_values:
            text = str(raw or "").strip()
            if not text:
                continue

            variants = {text, text.replace("\\", "/")}
            try:
                path_like = Path(text.replace("\\", "/"))
                variants.add(path_like.name)
                variants.add(path_like.stem)
            except Exception:
                pass

            for variant in variants:
                normalized = normalize_lora_key(variant)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                yield normalized

    def _build_lora_lookup(self, catalog: dict) -> dict:
        lookup = {}
        for key, entry in catalog.items():
            for normalized in self._iter_lora_lookup_keys(entry):
                lookup.setdefault(normalized, key)
        return lookup

    @staticmethod
    def _build_lora_entry(
        display_name: str,
        *,
        workflow_name: str = "",
        loader_name: str = "",
        default_strength=None,
        default_clip_strength=None,
        default_active: bool = False,
    ) -> dict:
        return {
            "name": display_name,
            "display_name": display_name,
            "workflow_name": workflow_name or "",
            "loader_name": loader_name or "",
            "default_strength": default_strength,
            "default_clip_strength": default_clip_strength,
            "default_active": bool(default_active),
            "aliases": [],
            "description": "",
            "trigger_words": [],
            "prompt_hints": [],
            "trigger_options": [],
            "source_nodes": [],
            "model_name": "",
            "base_model": "",
            "tags": [],
            "is_style_lora": False,
        }

    def _merge_lora_entry(self, base: dict, incoming: dict):
        if not base.get("display_name") and incoming.get("display_name"):
            base["display_name"] = incoming["display_name"]
            base["name"] = incoming["display_name"]

        if incoming.get("workflow_name") and not base.get("workflow_name"):
            base["workflow_name"] = incoming["workflow_name"]
        if incoming.get("loader_name") and not base.get("loader_name"):
            base["loader_name"] = incoming["loader_name"]

        if incoming.get("default_active"):
            base["default_active"] = True
        if incoming.get("default_strength") not in (None, ""):
            base["default_strength"] = coerce_strength(
                incoming.get("default_strength"), base.get("default_strength", 1.0)
            )
        if incoming.get("default_clip_strength") not in (None, ""):
            base["default_clip_strength"] = coerce_strength(
                incoming.get("default_clip_strength"),
                base.get("default_clip_strength", 1.0),
            )

        for field in (
            "aliases",
            "trigger_words",
            "prompt_hints",
            "trigger_options",
            "source_nodes",
            "tags",
        ):
            merged = [
                str(item).strip()
                for item in list(base.get(field, [])) + list(incoming.get(field, []))
                if str(item).strip()
            ]
            base[field] = list(dict.fromkeys(merged))

        if incoming.get("description") and not base.get("description"):
            base["description"] = str(incoming["description"]).strip()
        if incoming.get("model_name") and not base.get("model_name"):
            base["model_name"] = str(incoming["model_name"]).strip()
        if incoming.get("base_model") and not base.get("base_model"):
            base["base_model"] = str(incoming["base_model"]).strip()
        if incoming.get("is_style_lora"):
            base["is_style_lora"] = True

        return base

    def _extract_lora_catalog(self, workflow: dict):
        catalog = {}
        loader_node_ids = []

        for node_id, node_data in find_lora_loader_nodes(workflow):
            loader_node_ids.append(node_id)
            entries = node_data.get("inputs", {}).get("loras", {}).get("__value__", [])
            for item in entries:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue

                entry = catalog.setdefault(
                    name,
                    self._build_lora_entry(
                        name,
                        workflow_name=name,
                        default_strength=coerce_strength(item.get("strength"), 1.0),
                        default_clip_strength=coerce_strength(
                            item.get("clipStrength"), item.get("strength") or 1.0
                        ),
                        default_active=bool(item.get("active", False)),
                    ),
                )

                if item.get("active"):
                    entry["default_active"] = True
                if node_id not in entry["source_nodes"]:
                    entry["source_nodes"].append(node_id)

        return {"catalog": catalog, "loader_node_ids": loader_node_ids}

    def _load_lora_manifest(self) -> dict:
        path = self.workflow.manifest_path
        if not path.exists():
            return {}

        try:
            data = self._read_json_file(path)
        except Exception as e:
            logger.warning(f"[ComfyUI] failed to load LoRA sidecar {path.name}: {e}")
            return {}

        if not isinstance(data, dict):
            return {}

        loras = data.get("loras", data)
        return loras if isinstance(loras, dict) else {}

    def _extract_metadata_trigger_options(self, metadata: dict) -> list:
        civitai = metadata.get("civitai", {}) or {}
        raw_values = civitai.get("trainedWords", []) or []

        options = []
        for raw in raw_values:
            options.extend(self._split_trigger_options(raw))

        cleaned = []
        seen = set()
        for item in options:
            text = re.sub(r"\s+", " ", str(item or "").strip()).strip(",; ")
            normalized = self._normalize_prompt_token(text)
            if not text or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(text)
        return cleaned

    def _scan_lora_catalog(self) -> list:
        cache_key = tuple(str(path) for path in self.scan_roots)
        now = time.monotonic()
        if (
            self._lora_scan_cache_key == cache_key
            and self._lora_scan_cache is not None
            and now - self._lora_scan_cache_at < LORA_CONTEXT_CACHE_SECONDS
        ):
            return self._lora_scan_cache

        entries = []
        seen_files = set()

        for root in self.scan_roots:
            if not root.exists() or not root.is_dir():
                logger.warning(f"[ComfyUI] LoRA scan root does not exist: {root}")
                continue

            for path in root.rglob("*"):
                if (
                    not path.is_file()
                    or path.suffix.lower() not in LORA_FILE_EXTENSIONS
                ):
                    continue

                resolved_path = os.path.normcase(str(path.resolve()))
                if resolved_path in seen_files:
                    continue
                seen_files.add(resolved_path)

                try:
                    loader_name = path.relative_to(root).as_posix()
                except ValueError:
                    loader_name = path.name

                metadata_path = path.with_suffix(".metadata.json")
                metadata = {}
                if metadata_path.exists():
                    try:
                        metadata = self._read_json_file(metadata_path)
                    except Exception as e:
                        logger.warning(
                            f"[ComfyUI] failed to read metadata "
                            f"{metadata_path.name}: {e}"
                        )
                        metadata = {}

                file_name = (
                    str(metadata.get("file_name") or path.stem).strip() or path.stem
                )
                model_name = str(metadata.get("model_name") or "").strip()
                notes = str(metadata.get("notes") or "").strip()
                base_model = str(metadata.get("base_model") or "").strip()
                civitai = metadata.get("civitai", {}) or {}
                civitai_model = civitai.get("model", {}) or {}
                tag_values = []
                for source in (metadata.get("tags", []), civitai_model.get("tags", [])):
                    tag_values.extend(
                        str(item).strip() for item in source if str(item).strip()
                    )

                description = (
                    notes or model_name or str(civitai_model.get("name") or "").strip()
                )
                trigger_options = self._extract_metadata_trigger_options(metadata)
                aliases = [path.stem]
                if model_name and model_name != file_name:
                    aliases.append(model_name)
                if loader_name and loader_name not in aliases:
                    aliases.append(loader_name)

                tag_text = {self._normalize_prompt_token(tag) for tag in tag_values}
                entry = self._build_lora_entry(file_name, loader_name=loader_name)
                entry["aliases"] = list(
                    dict.fromkeys(alias for alias in aliases if alias)
                )
                entry["description"] = description
                entry["model_name"] = model_name
                entry["base_model"] = base_model
                entry["tags"] = list(dict.fromkeys(tag_values))
                entry["trigger_options"] = trigger_options
                entry["is_style_lora"] = (
                    "style" in tag_text
                    or self._normalize_prompt_token(model_name).endswith("/ style")
                )
                entries.append(entry)

        self._lora_scan_cache = entries
        self._lora_scan_cache_key = cache_key
        self._lora_scan_cache_at = now
        return entries

    def _merge_scanned_lora_catalog(self, catalog: dict):
        if not self.scan_roots:
            return catalog

        lookup = self._build_lora_lookup(catalog)

        for entry in self._scan_lora_catalog():
            matched_key = None
            for normalized in self._iter_lora_lookup_keys(entry):
                matched_key = lookup.get(normalized)
                if matched_key:
                    break

            if matched_key:
                self._merge_lora_entry(catalog[matched_key], entry)
            else:
                unique_key = entry.get("loader_name") or entry.get("display_name")
                suffix = 1
                while unique_key in catalog:
                    suffix += 1
                    base_name = entry.get("loader_name") or entry.get("display_name")
                    unique_key = f"{base_name}#{suffix}"
                catalog[unique_key] = entry

            lookup = self._build_lora_lookup(catalog)

        return catalog

    def _apply_manifest_metadata(self, catalog: dict):
        manifest = self._load_lora_manifest()
        if not manifest:
            return catalog

        lookup = self._build_lora_lookup(catalog)
        for raw_name, meta in manifest.items():
            if not isinstance(meta, dict):
                continue

            name = str(raw_name).strip()
            if not name:
                continue

            matched_key = lookup.get(normalize_lora_key(name))
            if matched_key:
                entry = catalog[matched_key]
            else:
                entry = self._build_lora_entry(name)
                catalog[name] = entry
                matched_key = name

            aliases = [
                str(alias).strip()
                for alias in meta.get("aliases", [])
                if str(alias).strip()
            ]
            trigger_words = [
                str(word).strip()
                for word in meta.get("trigger_words", [])
                if str(word).strip()
            ]
            prompt_hints = [
                str(word).strip()
                for word in meta.get("prompt_hints", [])
                if str(word).strip()
            ]

            incoming = self._build_lora_entry(
                entry.get("display_name") or name,
                workflow_name=entry.get("workflow_name", ""),
                loader_name=entry.get("loader_name", ""),
                default_strength=coerce_strength(
                    meta.get("default_strength"), entry.get("default_strength", 1.0)
                ),
                default_clip_strength=coerce_strength(
                    meta.get("default_clip_strength"),
                    entry.get("default_clip_strength", 1.0),
                ),
                default_active=entry.get("default_active", False),
            )
            incoming["aliases"] = aliases
            incoming["description"] = str(meta.get("description") or "").strip()
            incoming["trigger_words"] = trigger_words
            incoming["prompt_hints"] = prompt_hints
            incoming["trigger_options"] = trigger_words
            self._merge_lora_entry(entry, incoming)
            lookup = self._build_lora_lookup(catalog)

        return catalog

    def runtime_context(self) -> dict[str, Any]:
        """返回当前工作流的 LoRA 运行时上下文。

        Returns:
            目录、加载节点、默认激活项与扫描根等供命令和提示词使用的信息。
        """
        cache_key = (
            str(self.workflow.path),
            tuple(str(path) for path in self.scan_roots),
        )
        now = time.monotonic()
        if (
            self._lora_context_cache_key == cache_key
            and self._lora_context_cache is not None
            and now - self._lora_context_cache_at < LORA_CONTEXT_CACHE_SECONDS
        ):
            return self._lora_context_cache

        try:
            workflow = self.workflow.load()
        except Exception as e:
            logger.warning(f"[ComfyUI] failed to read workflow for LoRA context: {e}")
            return {
                "supported": False,
                "catalog": {},
                "loader_node_ids": [],
                "manifest_path": str(self.workflow.manifest_path),
            }

        extracted = self._extract_lora_catalog(workflow)
        catalog = extracted["catalog"]
        self._merge_scanned_lora_catalog(catalog)
        self._apply_manifest_metadata(catalog)

        for entry in catalog.values():
            entry["default_strength"] = coerce_strength(
                entry.get("default_strength"), 1.0
            )
            entry["default_clip_strength"] = coerce_strength(
                entry.get("default_clip_strength"), entry["default_strength"]
            )

        sorted_items = sorted(
            catalog.items(),
            key=lambda item: (
                not bool(item[1].get("default_active")),
                normalize_lora_key(item[1].get("display_name") or item[0]),
            ),
        )
        sorted_catalog = dict(sorted_items)
        default_active_loras = [
            entry for entry in sorted_catalog.values() if entry.get("default_active")
        ]

        context = {
            "supported": bool(extracted["loader_node_ids"]),
            "catalog": sorted_catalog,
            "loader_node_ids": extracted["loader_node_ids"],
            "manifest_path": str(self.workflow.manifest_path),
            "scan_roots": [str(path) for path in self.scan_roots],
            "default_active_loras": default_active_loras,
        }
        self._lora_context_cache = context
        self._lora_context_cache_key = cache_key
        self._lora_context_cache_at = now
        return context

    def prompt_appendix(self) -> str:
        """渲染注入 system prompt 的 LoRA 说明文本。

        Returns:
            说明文本，未启用或无需注入时为空串。
        """
        if not (
            self.settings.enabled and self.settings.inject_catalog_into_system_prompt
        ):
            return ""

        context = self.runtime_context()
        catalog = context.get("catalog", {})
        if not context.get("supported") or not catalog:
            return ""

        default_loras = context.get("default_active_loras", [])
        lines = [
            "--------------------------------------------------",
            "【五、可选 LoRA 堆控制】",
            "--------------------------------------------------",
            "",
            "当前工作流支持 LoRA 堆联动。",
            "如果当前画面确实需要特定 LoRA，"
            '请在对应的 `<pic prompt="...">` 之前额外输出一个标签：',
            '`<lora picks="LoRA名:强度@触发词序号, 另一个LoRA名:强度:clip强度@1+2">`',
            "",
            f"规则：最多选择 {self.settings.max_lora_count} 个；"
            "只能从下面清单里挑；不需要时不要输出 `<lora picks>`。",
            "如果想只在本次图片里禁用工作流默认 LoRA，请输出："
            f'`<lora picks="{LORA_CLEAR_TOKEN}">`。',
            "如果想禁用默认 LoRA 后改用别的 LoRA，请输出："
            f'`<lora picks="{LORA_CLEAR_TOKEN}, LoRA名:0.8@1">`。',
            "这里的 `@1`、`@1+2` 表示使用该 LoRA 下方列出来的"
            "第 1 个或第 1+2 个触发词候选。",
            "如果某个 LoRA 没有触发词候选，就把它视为全局风格 LoRA，"
            "直接选 LoRA 本体即可。",
            "重点规则：只要你写了 `@序号`，"
            "就表示系统会自动把该编号对应的触发词注入最终 prompt。",
            "因此，`<pic prompt>` 里不要再手动复述同一组触发词，"
            "不要把角色名、发色、服装、固定特征整段再写一遍。",
            "选择了 `@序号` 后，`<pic prompt>` 只写额外画面需求，"
            "例如构图、动作、表情、镜头、环境、光照、氛围。",
            "只有在触发词没有覆盖到某个关键信息时，才额外补少量缺失 tags；"
            "不要原样照抄触发词全文。",
            "",
        ]

        if default_loras:
            lines.append("当前工作流默认启用的 LoRA：")
            for entry in default_loras:
                strength_text = format_strength(entry.get("default_strength", 1.0))
                clip_text = format_strength(
                    entry.get(
                        "default_clip_strength", entry.get("default_strength", 1.0)
                    )
                )
                lines.append(
                    f"- {entry['display_name']} | "
                    f"default_on={strength_text}/{clip_text}"
                )
        else:
            lines.append("当前工作流默认启用的 LoRA：无")

        lines.extend(["", "当前可用 LoRA 清单："])
        for entry in catalog.values():
            strength_text = format_strength(entry.get("default_strength", 1.0))
            clip_strength = entry.get(
                "default_clip_strength", entry.get("default_strength", 1.0)
            )
            clip_text = format_strength(clip_strength)
            default_flag = "on" if entry.get("default_active") else "off"
            alias_values = [
                alias
                for alias in entry.get("aliases", [])
                if normalize_lora_key(alias)
                != normalize_lora_key(entry.get("display_name"))
            ]
            alias_preview = ", ".join(alias_values[:2])
            trigger_options = entry.get("trigger_options", [])[
                : self.settings.max_trigger_options_per_lora
            ]
            preview_length = self.settings.trigger_option_preview_length
            trigger_preview = " ; ".join(
                f"[{idx}] {self._trim_preview_text(option, preview_length)}"
                for idx, option in enumerate(trigger_options, start=1)
            )

            details = [f"default={default_flag}:{strength_text}/{clip_text}"]
            if alias_preview:
                details.append(f"aliases={alias_preview}")
            if entry.get("model_name"):
                details.append(
                    f"model={self._trim_preview_text(entry['model_name'], 48)}"
                )
            if entry.get("base_model"):
                details.append(f"base={entry['base_model']}")
            if trigger_preview:
                details.append(f"triggers={trigger_preview}")
            elif entry.get("prompt_hints"):
                details.append(
                    "hints="
                    + " ; ".join(
                        self._trim_preview_text(
                            text, self.settings.trigger_option_preview_length
                        )
                        for text in entry.get("prompt_hints", [])[
                            : self.settings.max_trigger_options_per_lora
                        ]
                    )
                )
            elif entry.get("is_style_lora"):
                details.append("triggers=无（更偏全局风格）")
            else:
                details.append("triggers=无")
            if entry.get("description"):
                details.append(
                    f"note={self._trim_preview_text(entry['description'], 60)}"
                )
            lines.append(f"- {entry['display_name']} | " + " | ".join(details))

        manifest_path = context.get("manifest_path")
        if manifest_path or context.get("scan_roots"):
            lines.extend(
                [
                    "",
                ]
            )
        if context.get("scan_roots"):
            lines.append("LoRA 扫描根目录：")
            for root in context.get("scan_roots", []):
                lines.append(f"- {root}")
        if manifest_path:
            lines.extend(
                [
                    f"工作流 sidecar 元数据文件：{manifest_path}",
                    "如果某个 LoRA 有额外 aliases、trigger_words、prompt_hints、"
                    "description，请按这些信息理解。",
                ]
            )

        return "\n".join(lines).strip()

    def resolve_selections(
        self,
        selections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把 LLM 给出的 LoRA 选择解析成可写入工作流的条目。

        Args:
            selections: 解析出的原始选择。

        Returns:
            带强度与触发词的最终选择。
        """
        if not selections:
            return []

        context = self.runtime_context()
        catalog = context.get("catalog", {})
        if not context.get("supported") or not catalog:
            return []

        lookup = self._build_lora_lookup(catalog)

        resolved = []
        seen = set()
        clear_defaults = False
        for item in selections:
            if not isinstance(item, dict):
                continue

            raw_name = str(item.get("name", "")).strip()
            if not raw_name:
                continue

            normalized_name = normalize_lora_key(raw_name)
            if item.get(
                "control"
            ) == "clear_defaults" or normalized_name == normalize_lora_key(
                LORA_CLEAR_TOKEN
            ):
                clear_defaults = True
                continue

            matched_key = lookup.get(normalized_name)
            if not matched_key:
                logger.info(f"[ComfyUI] LoRA not found in current workflow: {raw_name}")
                continue
            if matched_key in seen:
                continue

            entry = catalog[matched_key]
            strength = coerce_strength(
                item.get("strength"), entry.get("default_strength", 1.0)
            )
            clip_strength = coerce_strength(
                item.get("clip_strength"), entry.get("default_clip_strength", strength)
            )
            valid_trigger_indexes = []
            selected_prompt_hints = []
            for index in item.get("trigger_indexes", []) or []:
                try:
                    trigger_index = int(index)
                except (TypeError, ValueError):
                    continue
                if trigger_index < 1 or trigger_index > len(
                    entry.get("trigger_options", [])
                ):
                    continue
                valid_trigger_indexes.append(trigger_index)
                selected_prompt_hints.append(
                    entry["trigger_options"][trigger_index - 1]
                )

            if not selected_prompt_hints:
                if entry.get("prompt_hints"):
                    selected_prompt_hints = list(entry.get("prompt_hints", []))
                elif len(entry.get("trigger_options", [])) == 1:
                    selected_prompt_hints = [entry["trigger_options"][0]]

            resolved.append(
                {
                    "name": entry.get("workflow_name")
                    or entry.get("loader_name")
                    or entry.get("display_name"),
                    "display_name": entry.get("display_name") or raw_name,
                    "workflow_name": entry.get("workflow_name") or "",
                    "loader_name": entry.get("loader_name") or "",
                    "requested_name": raw_name,
                    "strength": strength,
                    "clip_strength": clip_strength,
                    "aliases": entry.get("aliases", []),
                    "trigger_words": entry.get("trigger_words", []),
                    "prompt_hints": entry.get("prompt_hints", []),
                    "trigger_options": entry.get("trigger_options", []),
                    "trigger_indexes": valid_trigger_indexes,
                    "selected_prompt_hints": selected_prompt_hints,
                    "description": entry.get("description", ""),
                }
            )
            seen.add(matched_key)

            if len(resolved) >= self.settings.max_lora_count:
                break

        if clear_defaults:
            resolved.insert(0, {"name": LORA_CLEAR_TOKEN, "control": "clear_defaults"})
        return resolved

    def inject_prompt_hints(
        self,
        prompt: str,
        selections: list[dict[str, Any]],
    ) -> tuple[str, list[str]]:
        """把选中 LoRA 的触发词拼进正面提示词。

        Args:
            prompt: 原始正面提示词。
            selections: 已解析的 LoRA 选择。

        Returns:
            拼好的提示词与实际注入的触发词。
        """
        if not (self.settings.inject_selected_prompt_hints and selections):
            return prompt, []

        existing_tokens = {
            self._normalize_prompt_token(token)
            for token in str(prompt or "").split(",")
            if str(token).strip()
        }

        injected = []
        for item in selections:
            if item.get("control"):
                continue
            selected_tokens = self._expand_prompt_hint_tokens(
                item.get("selected_prompt_hints", []) or item.get("prompt_hints", [])
            )
            for token in selected_tokens:
                cleaned = str(token).strip()
                normalized = self._normalize_prompt_token(cleaned)
                if not cleaned or not normalized or normalized in existing_tokens:
                    continue
                injected.append(cleaned)
                existing_tokens.add(normalized)

        if not injected:
            return prompt, []

        prefix = ", ".join(injected)
        final_prompt = f"{prefix}, {str(prompt or '').strip()}".strip(", ").strip()
        return final_prompt, injected
