"""插件数据目录的准备与工作流目录里的文件分类。"""

from __future__ import annotations

import shutil
from pathlib import Path

from astrbot.api import logger

from .contract import CONTRACT_SUFFIX

LEGACY_SUFFIXES = (".steps.json", ".lora.json")


def prepare_data_dir(data_dir: Path, plugin_dir: Path) -> None:
    """创建工作流与产物目录，首次安装时复制默认敏感词库。

    Args:
        data_dir: 插件数据目录。
        plugin_dir: 插件源目录，用于读取默认敏感词库。

    Raises:
        OSError: 目录或文件无法创建。
    """
    (data_dir / "workflow").mkdir(parents=True, exist_ok=True)
    (data_dir / "output").mkdir(parents=True, exist_ok=True)

    destination = data_dir / "sensitive_words.json"
    if destination.exists():
        return
    shutil.copy2(plugin_dir / "sensitive_words.json", destination)
    logger.info("[ComfyUI] 已复制默认敏感词文件")


def is_aux_file(filename: str) -> bool:
    """判断是否是工作流的附属文件。

    Args:
        filename: 工作流目录里的文件名。

    Returns:
        契约、步数覆盖或 LoRA 清单文件返回 True。
    """
    return filename.endswith((CONTRACT_SUFFIX, *LEGACY_SUFFIXES))
