"""工作流工具：清单、查看、准入与后台任务回收。"""

import asyncio
import json
from pathlib import Path

from astrbot.core.star.star_handler import EventType, star_handlers_registry
from astrbot_plugin_comfy_runner.main import ComfyUIPlugin
from helpers import FakeComfyServer, FakeEvent, make_plugin, run_tool, tool_of

WORKFLOW = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
}

CONTRACT = {
    "version": 1,
    "name": "comfy_moss_t2i",
    "description": "使用 MOSS 工作流根据提示词生成图片",
    "enabled": True,
    "inputs": {
        "prompt": {
            "type": "string",
            "description": "提示词",
            "required": True,
            "minLength": 1,
            "target": {"node": "1", "field": "text"},
        },
    },
    "outputs": {
        "image": {"type": "image", "target": {"node": "3", "field": "images"}},
    },
}

HISTORY = {
    "p1": {
        "status": {"status_str": "success", "completed": True},
        "outputs": {
            "3": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]},
        },
    }
}


def setup(tmp_path: Path, monkeypatch, control: dict | None = None):
    """写一对启用契约与一对禁用契约、装配插件并接管 HTTP。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    hidden = json.loads(json.dumps(CONTRACT))
    hidden.update(enabled=False, name="comfy_hidden")
    for stem, contract in (("moss", CONTRACT), ("hidden", hidden)):
        (workflow_dir / f"{stem}.json").write_text(
            json.dumps(WORKFLOW),
            encoding="utf-8",
        )
        (workflow_dir / f"{stem}.tool.json").write_text(
            json.dumps(contract),
            encoding="utf-8",
        )
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control=control
        or {"admin_ids": ["9"], "whitelist_ids": ["2"], "cooldown_seconds": 0},
    )
    server = FakeComfyServer().install(monkeypatch)
    server.history_reply = HISTORY
    return plugin, server


def test_list_returns_enabled_workflows(tmp_path, monkeypatch):
    """清单只返回启用的工作流名称与说明。"""
    plugin, _ = setup(tmp_path, monkeypatch)

    summary = run_tool(plugin, "comfy_list", FakeEvent(sender="9", group="2"))

    assert json.loads(summary) == [
        {
            "name": "comfy_moss_t2i",
            "description": "使用 MOSS 工作流根据提示词生成图片",
        }
    ]


def test_inspect_returns_public_contract(tmp_path, monkeypatch):
    """查看带回参数 Schema 与产物类型。"""
    plugin, _ = setup(tmp_path, monkeypatch)

    summary = run_tool(
        plugin,
        "comfy_inspect",
        FakeEvent(sender="9", group="2"),
        workflow="comfy_moss_t2i",
    )

    view = json.loads(summary)
    assert view["name"] == "comfy_moss_t2i"
    assert view["parameters"] == {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "提示词", "minLength": 1},
        },
        "required": ["prompt"],
        "additionalProperties": False,
    }
    assert view["outputs"] == {"image": {"type": "image"}}


def test_inspect_shows_optional_default_from_workflow(tmp_path, monkeypatch):
    """可选参数带工作流原值作为默认值，必填参数不带。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    contract = json.loads(json.dumps(CONTRACT))
    contract["inputs"].update(
        seed={
            "type": "integer",
            "description": "随机种子",
            "target": {"node": "3", "field": "seed"},
        }
    )
    workflow = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "3": {"class_type": "KSampler", "inputs": {"seed": 4242}},
        "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0]}},
    }
    (workflow_dir / "moss.json").write_text(json.dumps(workflow), encoding="utf-8")
    (workflow_dir / "moss.tool.json").write_text(
        json.dumps(contract),
        encoding="utf-8",
    )
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={"admin_ids": ["9"], "whitelist_ids": ["2"]},
    )
    event = FakeEvent(sender="9", group="2")

    view = json.loads(
        run_tool(plugin, "comfy_inspect", event, workflow="comfy_moss_t2i")
    )

    assert view["parameters"]["properties"]["seed"]["default"] == 4242
    assert "default" not in view["parameters"]["properties"]["prompt"]


def test_inspect_reads_default_from_current_workflow(tmp_path, monkeypatch):
    """工作流文件改动后，查看到的默认值跟着变。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    contract = json.loads(json.dumps(CONTRACT))
    contract["inputs"].update(
        seed={
            "type": "integer",
            "description": "随机种子",
            "target": {"node": "3", "field": "seed"},
        }
    )
    workflow = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
        "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0]}},
    }
    (workflow_dir / "moss.json").write_text(json.dumps(workflow), encoding="utf-8")
    (workflow_dir / "moss.tool.json").write_text(
        json.dumps(contract),
        encoding="utf-8",
    )
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={"admin_ids": ["9"], "whitelist_ids": ["2"]},
    )
    event = FakeEvent(sender="9", group="2")

    workflow["3"]["inputs"]["seed"] = 77
    (workflow_dir / "moss.json").write_text(json.dumps(workflow), encoding="utf-8")
    view = json.loads(
        run_tool(plugin, "comfy_inspect", event, workflow="comfy_moss_t2i")
    )

    assert view["parameters"]["properties"]["seed"]["default"] == 77


def test_inspect_hides_node_mapping(tmp_path, monkeypatch):
    """查看结果不含节点 ID、字段名与文件路径。"""
    plugin, _ = setup(tmp_path, monkeypatch)

    summary = run_tool(
        plugin,
        "comfy_inspect",
        FakeEvent(sender="9", group="2"),
        workflow="comfy_moss_t2i",
    )

    assert "target" not in summary
    assert "1.text" not in summary
    assert str(tmp_path) not in summary


def test_inspect_unknown_workflow_lists_available_names(tmp_path, monkeypatch):
    """未知工作流返回错误，并提示可用名称。"""
    plugin, _ = setup(tmp_path, monkeypatch)

    summary = run_tool(
        plugin,
        "comfy_inspect",
        FakeEvent(sender="9", group="2"),
        workflow="comfy_nope",
    )

    assert summary.startswith("❌")
    assert "未知的工作流: comfy_nope" in summary
    assert "comfy_moss_t2i" in summary


def test_disabled_workflow_is_not_discoverable(tmp_path, monkeypatch):
    """禁用的工作流既不出现在清单里，也不能查看。"""
    plugin, _ = setup(tmp_path, monkeypatch)
    event = FakeEvent(sender="9", group="2")

    summary = run_tool(plugin, "comfy_inspect", event, workflow="comfy_hidden")

    assert "未知的工作流: comfy_hidden" in summary
    assert "comfy_hidden" not in run_tool(plugin, "comfy_list", event)


def test_unlisted_group_cannot_discover_workflows(tmp_path, monkeypatch):
    """白名单外的群拿不到任何工作流名称。"""
    plugin, _ = setup(tmp_path, monkeypatch)

    summary = run_tool(plugin, "comfy_list", FakeEvent(sender="5", group="8"))

    assert "不在白名单中" in summary
    assert "comfy_moss_t2i" not in summary


def test_lockdown_rejects_discovery_for_non_admin(tmp_path, monkeypatch):
    """全局锁定时普通用户无法查看工作流。"""
    plugin, _ = setup(
        tmp_path,
        monkeypatch,
        control={
            "admin_ids": ["9"],
            "whitelist_ids": ["2"],
            "lockdown": True,
        },
    )

    summary = run_tool(plugin, "comfy_list", FakeEvent(sender="5", group="2"))

    assert "全局锁定" in summary


def test_terminate_cancels_pending_delivery(tmp_path, monkeypatch):
    """插件卸载时取消尚未完成的后台交付并归还并发名额。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.history_reply = {}
    event = FakeEvent(sender="9", group="2")

    async def scenario() -> str:
        tool = tool_of(plugin, "comfy_execute")
        summary = await tool.handler(
            event,
            workflow="comfy_moss_t2i",
            arguments={"prompt": "一只猫"},
        )
        await plugin.terminate()
        return summary

    summary = asyncio.run(scenario())

    assert "已提交" in summary
    assert plugin.jobs.tasks == ()
    assert plugin.gate.active == 0


def test_plugin_registers_no_chat_hooks():
    """插件不注册对话钩子，普通聊天不会被插入说明或触发执行。"""
    module = ComfyUIPlugin.__module__
    bound = {
        handler.event_type
        for handler in star_handlers_registry
        if handler.handler_module_path == module
    }

    assert EventType.OnLLMRequestEvent not in bound
    assert EventType.OnLLMResponseEvent not in bound
    assert EventType.OnDecoratingResultEvent not in bound
