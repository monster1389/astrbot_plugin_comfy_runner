"""工作流选择的持久化与 WebUI 配置冲突判定。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

STATE_FILENAME = "state.json"


class StateError(ValueError):
    """state.json 缺失字段或内容不合法。"""


@dataclass(frozen=True, slots=True)
class WorkflowState:
    """当前工作流与上次读到的 WebUI 配置值。"""

    workflow: str
    config_value: str


def load_state(path: Path) -> WorkflowState | None:
    """读取工作流状态。

    Args:
        path: state.json 路径。

    Returns:
        状态对象；文件不存在时返回 None。

    Raises:
        StateError: 文件无法读取或内容不合法。
    """
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"读取 {path} 失败: {exc}") from exc

    if not isinstance(data, dict):
        raise StateError(f"{path} 必须是对象")
    workflow = data.get("workflow")
    config_value = data.get("config_value")
    if not isinstance(workflow, str) or not isinstance(config_value, str):
        raise StateError(f"{path} 必须包含字符串 workflow 和 config_value")
    return WorkflowState(workflow=workflow, config_value=config_value)


def save_state(path: Path, state: WorkflowState) -> None:
    """原子写入工作流状态。

    Args:
        path: state.json 路径。
        state: 要保存的状态。

    Raises:
        OSError: 文件无法写入。
    """
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "workflow": state.workflow,
                "config_value": state.config_value,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def resolve_workflow(
    config_value: str,
    state: WorkflowState | None,
) -> WorkflowState:
    """按当前 WebUI 配置和旧状态决定实际工作流。

    Args:
        config_value: WebUI 配置里的 json_file。
        state: 上次保存的状态，可能为空。

    Returns:
        应保存并使用的状态。
    """
    if state is None or state.config_value != config_value:
        return WorkflowState(workflow=config_value, config_value=config_value)
    return state
