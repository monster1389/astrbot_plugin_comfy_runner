"""WebUI 工作流下拉反映数据目录的当前内容。"""

import json
from pathlib import Path

import pytest
from helpers import FakeComfyServer, FakeEvent, collect, make_plugin

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
        workflow_settings={
            "json_file": "a.json",
            "bindings": json.dumps({"a.json": {"input": "1"}}),
        },
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    item = config.schema["workflow_settings"]["items"]["json_file"]
    assert item["options"] == ["a.json", "b.json"]
    assert item["enum"] == ["a.json", "b.json"]

    listing = collect(plugin.cmd_comfy_list(FakeEvent("/comfy_ls", sender="9")))
    assert "a.json [绑定]" in listing[0][1]
    assert "b.json [绑定]" not in listing[0][1]
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
            "bindings": json.dumps({"wf.json": {"input": 1, "neg": 2, "output": 4}}),
        },
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

    results = collect(plugin.cmd_paint(FakeEvent("/画图 one girl, smile")))

    graph = server.submissions[0]
    assert results[0][0] == "chain"
    assert type(results[0][1][0]).__name__ == "Node"
    assert graph["1"]["inputs"]["text"] == "one girl, smile"
    assert graph["3"]["inputs"]["seed"] == 1
    assert len(list((tmp_path / "plugin_data" / "output").glob("*.png"))) == 1


@pytest.mark.parametrize(
    ("filename", "bindings", "expected"),
    [
        ("missing.json", "{}", "未在 bindings 中登记"),
        (
            "missing.json",
            json.dumps({"missing.json": {"input": "6"}}),
            "/comfy ls",
        ),
        ("", "{}", "/comfy ls"),
    ],
)
def test_paint_reports_missing_workflow(
    tmp_path,
    monkeypatch,
    filename,
    bindings,
    expected,
):
    """/画图 在工作流未配置、未登记或文件缺失时给出明确错误。"""
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        workflow_settings={"json_file": filename, "bindings": bindings},
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    server = FakeComfyServer().install(monkeypatch)

    results = collect(plugin.cmd_paint(FakeEvent("/画图 one girl")))

    assert results[0][0] == "text"
    assert expected in results[0][1]
    assert server.submissions == []


def test_switch_applies_each_workflow_binding(tmp_path, monkeypatch):
    """切换工作流后使用该工作流自己的正面与输出节点。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "a.json").write_text(
        FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    second = {
        "10": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "11": {
            "class_type": "KSampler",
            "inputs": {"seed": 1, "steps": 20, "positive": ["10", 0]},
        },
        "20": {"class_type": "SaveImage", "inputs": {"images": ["11", 0]}},
    }
    (workflow_dir / "b.json").write_text(json.dumps(second), encoding="utf-8")

    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        workflow_settings={
            "json_file": "a.json",
            "bindings": json.dumps(
                {
                    "a.json": {"input": "1", "output": "4"},
                    "b.json": {"input": "10", "output": "20"},
                }
            ),
        },
        control={
            "admin_ids": ["9"],
            "whitelist_group_ids": ["2"],
            "cooldown_seconds": 0,
        },
    )
    server = FakeComfyServer().install(monkeypatch)
    server.history_reply = {
        "p1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "4": {
                    "images": [{"filename": "a.png", "subfolder": "", "type": "out"}]
                },
                "20": {
                    "images": [{"filename": "b.png", "subfolder": "", "type": "out"}]
                },
            },
        }
    }

    collect(plugin.cmd_paint(FakeEvent("/画图 first", sender="5", group="2")))
    collect(plugin.cmd_comfy_use(FakeEvent("/comfy_use 2", sender="9", group="2")))
    collect(plugin.cmd_paint(FakeEvent("/画图 second", sender="5", group="2")))

    first_graph, second_graph = server.submissions
    assert first_graph["1"]["inputs"]["text"] == "first"
    assert second_graph["10"]["inputs"]["text"] == "second"


def test_unregistered_workflow_is_rejected(tmp_path, monkeypatch):
    """未登记绑定的工作流不出图，并指向登记位置。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True)
    _touch(workflow_dir / "a.json")
    _touch(workflow_dir / "b.json")

    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        workflow_settings={
            "json_file": "a.json",
            "bindings": json.dumps({"a.json": {"input": "1"}}),
        },
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    server = FakeComfyServer().install(monkeypatch)

    switched = collect(
        plugin.cmd_comfy_use(FakeEvent("/comfy_use 2", sender="9", group="2"))
    )
    assert "未在 bindings 中登记" in switched[0][1]

    current = collect(plugin.cmd_comfy_current(FakeEvent("/当前工作流", sender="9")))
    assert "未登记" in current[0][1]

    results = collect(
        plugin.cmd_paint(FakeEvent("/画图 one girl", sender="5", group="2"))
    )

    assert results[0][0] == "text"
    assert "未在 bindings 中登记" in results[0][1]
    assert server.submissions == []


def test_use_workflow_rejects_node_id_arguments(tmp_path, monkeypatch):
    """ID 参数已移除，命令指向配置字段。"""
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )

    results = collect(plugin.cmd_comfy_use(FakeEvent("/comfy_use 1 6 7 9", sender="9")))

    assert results[0][0] == "text"
    assert "bindings" in results[0][1]
