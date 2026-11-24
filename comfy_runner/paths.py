"""插件数据目录的准备、临时产物路径解析与清理。"""

from __future__ import annotations

import shutil
from pathlib import Path

from astrbot.api import logger

from .contract import CONTRACT_SUFFIX

PLUGIN_DIR = Path(__file__).resolve().parents[1]
LEGACY_SUFFIXES = (".steps.json", ".lora.json")


def prepare_data_dir(data_dir: Path, plugin_dir: Path) -> None:
    """创建工作流与临时目录，清理残留临时文件并在首次安装时复制默认敏感词库。

    Args:
        data_dir: 插件数据目录。
        plugin_dir: 插件源目录，用于读取默认敏感词库。

    Raises:
        OSError: 目录或文件无法创建。
    """
    (data_dir / "workflow").mkdir(parents=True, exist_ok=True)
    clear_temp_dir(data_dir / "temp")

    destination = data_dir / "sensitive_words.json"
    if destination.exists():
        return
    shutil.copy2(plugin_dir / "sensitive_words.json", destination)
    logger.info("[ComfyUI] 已复制默认敏感词文件")


def clear_temp_dir(temp_dir: Path) -> None:
    """清空并重建插件临时目录。

    Args:
        temp_dir: 临时目录路径。
    """
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)


def resolve_temp_path(temp_dir: Path, subfolder: str, filename: str) -> Path:
    """按 ComfyUI 的子目录与文件名解析临时目录内的目标路径。

    Args:
        temp_dir: 临时目录根目录。
        subfolder: ComfyUI 产物子目录。
        filename: ComfyUI 产物文件名。

    Returns:
        位于临时目录内的文件绝对路径，已确保父目录存在。

    Raises:
        ValueError: 文件名为空或路径企图越出临时目录。
    """
    name = filename.strip()
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError(f"非法的产物文件名: {filename!r}")

    clean_sub = subfolder.replace("\\", "/").strip("/")
    root = temp_dir.resolve()
    candidate = (root / clean_sub / name) if clean_sub else (root / name)
    resolved = candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"非法的产物路径: {subfolder}/{filename}")

    target = (temp_dir / clean_sub / name) if clean_sub else (temp_dir / name)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def cleanup_temp_artifact(temp_dir: Path, path: Path) -> None:
    """删除临时产物文件并回收其下空子目录。

    Args:
        temp_dir: 临时目录根目录。
        path: 待清理的临时文件路径。
    """
    path.unlink(missing_ok=True)
    root = temp_dir.resolve()
    current = path.parent
    while current.resolve() != root and current.resolve().is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def is_aux_file(filename: str) -> bool:
    """判断是否是工作流的附属文件。

    Args:
        filename: 工作流目录里的文件名。

    Returns:
        契约、步数覆盖或 LoRA 清单文件返回 True。
    """
    return filename.endswith((CONTRACT_SUFFIX, *LEGACY_SUFFIXES))
