"""WebUI 工作流下拉反映数据目录的当前内容。"""

from pathlib import Path

import pytest
from helpers import FakeEvent, collect, make_plugin

FIXTURE = Path(__file__).with_name("fixtures") / "workflow_min.json"


def _touch(path: Path) -> None:
    path.write_text("{}", encoding="utf-8")


def test_webui_lists_workflows_and_follows_save(tmp_path, monkeypatch):
    """WebUI 下拉列出工作流，重载后保留，导入后立即出现。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True)
    _touch(workflow_dir / "a.json")
    _touch(workflow_dir / "b.json")
    _touch(workflow_dir / "a.steps.json")
    _touch(workflow_dir / "a.lora.json")

    plugin, config = make_plugin(
        tmp_path,
        monkeypatch,
        workflow_settings={"json_file": "a.json"},
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    item = config.schema["workflow_settings"]["items"]["json_file"]
    assert item["options"] == ["a.json", "b.json"]
    assert item["enum"] == ["a.json", "b.json"]

    listing = collect(plugin.cmd_comfy_list(FakeEvent("/comfy_ls", sender="9")))
    assert "a.json" in listing[0][1]
    assert "b.json" in listing[0][1]
    assert "a.steps.json" not in listing[0][1]
    assert "a.lora.json" not in listing[0][1]

    event = FakeEvent("/comfy_save c.json {}", sender="9", group="2")
    collect(plugin.cmd_comfy_save(event))

    assert (workflow_dir / "c.json").exists()
    assert item["options"] == ["a.json", "b.json", "c.json"]

    _, reloaded = make_plugin(
        tmp_path,
        monkeypatch,
        workflow_settings={"json_file": "a.json"},
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    reloaded_item = reloaded.schema["workflow_settings"]["items"]["json_file"]
    assert reloaded_item["options"] == ["a.json", "b.json", "c.json"]


def test_use_workflow_persists_and_webui_change_wins(tmp_path, monkeypatch):
    """命令切换跨重启保持，WebUI 改配置后由 WebUI 接管。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True)
    _touch(workflow_dir / "a.json")
    _touch(workflow_dir / "b.json")
    _touch(workflow_dir / "c.json")

    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        workflow_settings={"json_file": "a.json"},
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    event = FakeEvent("/comfy_use 2", sender="9", group="2")
    collect(plugin.cmd_comfy_use(event))

    assert plugin.api.workflow.filename == "b.json"

    current = collect(plugin.cmd_comfy_current(FakeEvent("/当前工作流", sender="9")))
    assert "b.json" in current[0][1]

    reloaded, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    assert reloaded.api.workflow.filename == "b.json"

    reloaded.selection.raw_config["workflow_settings"]["json_file"] = "c.json"
    reloaded.selection.raw_config.save_config()

    overridden, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    assert overridden.api.workflow.filename == "c.json"


def test_paint_loads_workflow_and_injects_prompt(tmp_path, monkeypatch):
    """/画图 读取当前工作流并把提示词写进正面节点。"""
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
            "input_node_id": "1",
            "neg_node_id": "2",
            "output_node_id": "4",
        },
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    captured = {}

    async def fake_generate(graph, output_node_id):
        captured["graph"] = graph
        captured["output_node_id"] = output_node_id
        return b"png-bytes"

    monkeypatch.setattr(plugin.api.client, "generate", fake_generate)

    results = collect(plugin.cmd_paint(FakeEvent("/画图 one girl, smile")))

    assert results[0][0] == "chain"
    assert type(results[0][1][0]).__name__ == "Node"
    assert captured["graph"]["1"]["inputs"]["text"] == "one girl, smile"
    assert captured["graph"]["3"]["inputs"]["seed"] != 1
    assert captured["output_node_id"] == "4"
    assert len(list((tmp_path / "plugin_data" / "output").glob("*.png"))) == 1


@pytest.mark.parametrize("filename", ["missing.json", ""])
def test_paint_reports_missing_workflow(tmp_path, monkeypatch, filename):
    """/画图 在工作流未配置或文件缺失时给出明确错误。"""
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        workflow_settings={"json_file": filename},
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    calls = []

    async def fake_generate(graph, output_node_id):
        calls.append((graph, output_node_id))
        return b"unexpected"

    monkeypatch.setattr(plugin.api.client, "generate", fake_generate)

    results = collect(plugin.cmd_paint(FakeEvent("/画图 one girl")))

    assert results[0][0] == "text"
    assert "工作流" in results[0][1]
    assert "/comfy_ls" in results[0][1]
    assert calls == []
