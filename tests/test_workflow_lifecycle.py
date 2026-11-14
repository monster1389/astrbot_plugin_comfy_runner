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

    listing = collect(plugin.cmd_comfy_list(FakeEvent("/comfy ls", sender="9")))
    assert "⚠ a.json 无配套契约" in listing[0][1]
    assert "⚠ b.json 无配套契约" in listing[0][1]
    assert "a.steps.json" not in listing[0][1]
    assert "a.lora.json" not in listing[0][1]

    event = FakeEvent("/comfy save c.json {}", sender="9", group="2")
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




def test_policy_command_sets_group_level(tmp_path, monkeypatch):
    """/comfy policy 设置本群违禁级别。"""
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )

    results = collect(
        plugin.cmd_set_policy(FakeEvent("/comfy policy full", sender="9", group="2"))
    )

    assert "已将本群违禁级别设置为：full" in results[0][1]
    assert plugin.access.policy_for(FakeEvent(sender="5", group="2")) == "full"


def test_lock_command_toggles_lockdown(tmp_path, monkeypatch):
    """/comfy lock on|off 切换全局锁定。"""
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )

    on = collect(plugin.cmd_comfy_lock(FakeEvent("/comfy lock on", sender="9")))
    assert "已开启全局锁定" in on[0][1]
    assert plugin.access.lockdown is True

    off = collect(plugin.cmd_comfy_lock(FakeEvent("/comfy lock off", sender="9")))
    assert "已关闭全局锁定" in off[0][1]
    assert plugin.access.lockdown is False
