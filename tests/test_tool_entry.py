"""工具入口：产交付与准入拒绝的用户可见行为。"""

import asyncio
import json
from pathlib import Path

import pytest
from astrbot.core.star.star_handler import EventType, star_handlers_registry
from astrbot_plugin_comfy_runner.comfy_runner.tools import WorkflowRejected
from astrbot_plugin_comfy_runner.main import ComfyUIPlugin
from helpers import FakeComfyServer, FakeEvent, make_plugin

WORKFLOW = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    "4": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
}

CONTRACT = {
    "version": 1,
    "name": "comfy_test_t2i",
    "description": "测试用工作流",
    "enabled": True,
    "inputs": {
        "prompt": {
            "type": "string",
            "description": "提示词",
            "required": True,
            "target": {"node": "1", "field": "text"},
        }
    },
    "outputs": {
        "second": {"type": "image", "target": {"node": "4", "field": "images"}},
        "first": {"type": "image", "target": {"node": "3", "field": "images"}},
    },
}

HISTORY = {
    "p1": {
        "status": {"status_str": "success", "completed": True},
        "outputs": {
            "3": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]},
            "4": {"images": [{"filename": "b.png", "subfolder": "", "type": "output"}]},
        },
    }
}


def setup(tmp_path: Path, monkeypatch, control: dict | None = None):
    """写一对契约与工作流、装配插件并接管 HTTP。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "wf.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")
    (workflow_dir / "wf.tool.json").write_text(
        json.dumps(CONTRACT),
        encoding="utf-8",
    )
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control=control
        or {"admin_ids": ["9"], "whitelist_group_ids": ["2"], "cooldown_seconds": 0},
    )
    server = FakeComfyServer().install(monkeypatch)
    server.history_reply = HISTORY
    return plugin, server


def call(plugin, event, prompt: str = "一只猫"):
    """按 AstrBot 的调用方式执行一次工具。"""
    handler = plugin.function_tools[0].handler
    return asyncio.run(handler(event, prompt=prompt))


def test_all_artifacts_are_sent_to_current_session(tmp_path, monkeypatch):
    """一次调用产出的全部正式产物都在原会话收到。"""
    plugin, _ = setup(tmp_path, monkeypatch)
    event = FakeEvent(sender="9", group="2")

    summary = call(plugin, event)

    assert summary == "已生成 2 个产物并发送到当前会话"
    assert len(event.sent) == 1
    kind, components = event.sent[0]
    assert kind == "chain"
    assert [type(item).__name__ for item in components] == ["Image", "Image"]
    assert len(list((tmp_path / "plugin_data" / "output").glob("*.png"))) == 2


def test_summary_hides_internal_location(tmp_path, monkeypatch):
    """给模型的结果不含节点、路径或 ComfyUI 地址。"""
    plugin, _ = setup(tmp_path, monkeypatch)

    summary = call(plugin, FakeEvent(sender="9", group="2"))

    assert "http" not in summary
    assert str(tmp_path) not in summary
    assert "1.text" not in summary


def test_group_outside_whitelist_is_rejected(tmp_path, monkeypatch):
    """白名单外的群不提交工作流。"""
    plugin, server = setup(tmp_path, monkeypatch)

    with pytest.raises(WorkflowRejected, match="不在白名单中"):
        call(plugin, FakeEvent(sender="5", group="8"))

    assert server.submissions == []


def test_sensitive_prompt_is_rejected(tmp_path, monkeypatch):
    """参数命中敏感词时不提交工作流。"""
    plugin, server = setup(
        tmp_path,
        monkeypatch,
        control={
            "admin_ids": ["9"],
            "whitelist_group_ids": ["2"],
            "default_group_policy": "full",
        },
    )

    with pytest.raises(WorkflowRejected, match="检测到敏感词"):
        call(plugin, FakeEvent(sender="5", group="2"), prompt="blood, smile")

    assert server.submissions == []


def test_lockdown_rejects_non_admin(tmp_path, monkeypatch):
    """全局锁定时普通用户无法调用工作流工具。"""
    plugin, server = setup(
        tmp_path,
        monkeypatch,
        control={
            "admin_ids": ["9"],
            "whitelist_group_ids": ["2"],
            "lockdown": True,
        },
    )

    with pytest.raises(WorkflowRejected, match="全局锁定"):
        call(plugin, FakeEvent(sender="5", group="2"))

    assert server.submissions == []


def test_plugin_registers_no_chat_hooks():
    """插件不再注册对话钩子，普通聊天不会被插入说明或触发执行。"""
    module = ComfyUIPlugin.__module__
    bound = {
        handler.event_type
        for handler in star_handlers_registry
        if handler.handler_module_path == module
    }

    assert EventType.OnLLMRequestEvent not in bound
    assert EventType.OnLLMResponseEvent not in bound
    assert EventType.OnDecoratingResultEvent not in bound
