"""出图与多图发送的用户可见行为。"""

import asyncio
import json
from pathlib import Path

from astrbot.api.message_components import Plain
from helpers import FakeComfyServer, FakeEvent, FakeResult, collect, make_plugin

FIXTURE = Path(__file__).with_name("fixtures") / "workflow_min.json"


def test_paint_rejects_empty_prompt(tmp_path, monkeypatch):
    """没有提示词时不调用客户端。"""
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )

    results = collect(plugin.cmd_paint(FakeEvent("/画图", sender="9", group="2")))

    assert results[0][0] == "text"
    assert "/画图" in results[0][1]


def test_paint_rejects_sensitive_prompt(tmp_path, monkeypatch):
    """普通用户命中敏感词时拒绝出图。"""
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={
            "admin_ids": ["9"],
            "whitelist_group_ids": ["2"],
            "default_group_policy": "full",
        },
    )
    server = FakeComfyServer().install(monkeypatch)

    event = FakeEvent("/画图 blood, smile", sender="5", group="2")
    results = collect(plugin.cmd_paint(event))

    assert results[0][0] == "text"
    assert "敏感词" in results[0][1]
    assert server.submissions == []


def test_multi_image_sends_text_and_images_in_order(tmp_path, monkeypatch):
    """多图模式按文字段、图片交替发送。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "wf.json").write_text(
        FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        workflow_settings={
            "json_file": "wf.json",
            "bindings": json.dumps(
                {"wf.json": {"input": "1", "neg": "2", "output": "4"}}
            ),
        },
        llm_settings={"multi_image_mode": True},
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    server = FakeComfyServer().install(monkeypatch)
    server.history_reply = {
        "p1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "4": {
                    "images": [{"filename": "a.png", "subfolder": "", "type": "output"}]
                }
            },
        }
    }

    event = FakeEvent(sender="9", group="2")
    event._comfy_segments = [
        {"type": "text", "content": "第一段"},
        {"type": "prompt", "content": "one girl, smile", "lora_selections": []},
        {"type": "text", "content": "第二段"},
        {"type": "prompt", "content": "blue sky, clouds", "lora_selections": []},
    ]
    event.set_result(FakeResult([Plain("原始")]))

    asyncio.run(plugin._auto_paint_from_llm(event))
    asyncio.run(plugin._send_multi_image_results(event))

    assert [kind for kind, _ in event.sent] == ["chain", "chain", "chain", "chain"]
    assert [graph["1"]["inputs"]["text"] for graph in server.submissions] == [
        "one girl, smile",
        "blue sky, clouds",
    ]
    assert event.get_result().chain == []
    assert len(list((tmp_path / "plugin_data" / "output").glob("*.png"))) == 2
