"""管理命令的用户可见行为。"""

import json
from pathlib import Path

from helpers import FakeComfyServer, FakeEvent, collect, make_plugin

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
            "target": {"node": "1", "field": "text"},
        }
    },
    "outputs": {
        "image": {"type": "image", "target": {"node": "3", "field": "images"}},
    },
}


def admin_overrides() -> dict:
    """管理员在群 2 的访问配置。"""
    return {"admin_ids": ["9"], "whitelist_group_ids": ["2"]}


def write_workflow_dir(tmp_path: Path) -> Path:
    """写一份工作流与契约，返回工作流目录。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "moss.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")
    (workflow_dir / "moss.tool.json").write_text(
        json.dumps(CONTRACT),
        encoding="utf-8",
    )
    return workflow_dir


def test_save_command_imports_workflow_and_contract(tmp_path, monkeypatch):
    """/comfy save 写入工作流与契约文件。"""
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    workflow_dir = tmp_path / "plugin_data" / "workflow"

    workflow_event = FakeEvent(
        '/comfy save my_flow {"1": {}}',
        sender="9",
        group="2",
    )
    results = collect(plugin.cmd_comfy_save(workflow_event))

    assert "保存成功" in results[0][1]
    assert (workflow_dir / "my_flow.json").exists()

    contract_event = FakeEvent(
        f"/comfy save my_flow.tool.json {json.dumps(CONTRACT)}",
        sender="9",
        group="2",
    )
    collect(plugin.cmd_comfy_save(contract_event))

    assert (workflow_dir / "my_flow.tool.json").exists()


def test_save_command_rejects_broken_json(tmp_path, monkeypatch):
    """JSON 无法解析时不写文件。"""
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    workflow_dir = tmp_path / "plugin_data" / "workflow"

    results = collect(
        plugin.cmd_comfy_save(FakeEvent("/comfy save bad.json {", sender="9"))
    )

    assert "JSON 解析失败" in results[0][1]
    assert not (workflow_dir / "bad.json").exists()


def test_list_command_shows_pairing(tmp_path, monkeypatch):
    """/comfy ls 展示工作流与契约的配对、启用与校验状态。"""
    workflow_dir = write_workflow_dir(tmp_path)
    (workflow_dir / "plain.json").write_text("{}", encoding="utf-8")
    (workflow_dir / "moss.steps.json").write_text("{}", encoding="utf-8")
    orphan = dict(CONTRACT, name="comfy_orphan", enabled=False)
    (workflow_dir / "orphan.tool.json").write_text(
        json.dumps(orphan),
        encoding="utf-8",
    )

    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    results = collect(plugin.cmd_comfy_list(FakeEvent("/comfy ls", sender="9")))

    assert "✅ moss.json -> comfy_moss_t2i [启用]" in results[0][1]
    assert "⚠️ plain.json 无配套契约" in results[0][1]
    assert "❌ orphan.tool.json -> comfy_orphan [禁用] 缺少工作流 orphan.json" in (
        results[0][1]
    )
    assert "moss.steps.json" not in results[0][1]


def test_list_command_requires_admin(tmp_path, monkeypatch):
    """/comfy ls 仅管理员可用。"""
    write_workflow_dir(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    results = collect(plugin.cmd_comfy_list(FakeEvent("/comfy ls", sender="5")))

    assert "权限不足" in results[0][1]


def test_help_command_lists_enabled_tools(tmp_path, monkeypatch):
    """/comfy help 显示已启用的工作流工具。"""
    write_workflow_dir(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    results = collect(plugin.cmd_comfy_help(FakeEvent("/comfy help", sender="9")))

    assert "已启用工具：comfy_moss_t2i" in results[0][1]
    assert "/comfy check" in results[0][1]


def test_policy_command_sets_group_level(tmp_path, monkeypatch):
    """/comfy policy 设置本群违禁级别。"""
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    results = collect(
        plugin.cmd_set_policy(FakeEvent("/comfy policy full", sender="9", group="2"))
    )

    assert "已将本群违禁级别设置为：full" in results[0][1]
    assert plugin.access.policy_for(FakeEvent(sender="5", group="2")) == "full"


def test_policy_command_rejects_unknown_level(tmp_path, monkeypatch):
    """未知级别不生效。"""
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    results = collect(
        plugin.cmd_set_policy(FakeEvent("/comfy policy max", sender="9", group="2"))
    )

    assert "无效级别" in results[0][1]


def test_lock_command_toggles_lockdown(tmp_path, monkeypatch):
    """/comfy lock on|off 切换全局锁定。"""
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    on = collect(plugin.cmd_comfy_lock(FakeEvent("/comfy lock on", sender="9")))
    assert "已开启全局锁定" in on[0][1]
    assert plugin.access.lockdown is True

    off = collect(plugin.cmd_comfy_lock(FakeEvent("/comfy lock off", sender="9")))
    assert "已关闭全局锁定" in off[0][1]
    assert plugin.access.lockdown is False


def test_check_command_reports_contract_errors(tmp_path, monkeypatch):
    """/comfy check 报告契约问题并定位到参数与节点字段。"""
    workflow_dir = write_workflow_dir(tmp_path)
    broken = json.loads(json.dumps(CONTRACT))
    broken.update(name="comfy_broken", enabled=False)
    broken["inputs"]["prompt"]["target"] = {"node": "99", "field": "text"}
    (workflow_dir / "moss.tool.json").write_text(
        json.dumps(broken),
        encoding="utf-8",
    )

    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    results = collect(plugin.cmd_comfy_check(FakeEvent("/comfy check", sender="9")))

    assert "参数 prompt 的目标节点 99 不存在" in results[0][1]


def test_check_command_remote_uses_node_definitions(tmp_path, monkeypatch):
    """/comfy check remote 用 ComfyUI 节点定义核对目标字段。"""
    write_workflow_dir(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    server = FakeComfyServer().install(monkeypatch)
    server.object_info = {"CLIPTextEncode": {"input": {"required": {}}}}

    results = collect(
        plugin.cmd_comfy_check(FakeEvent("/comfy check remote", sender="9"))
    )

    assert "已附带 ComfyUI 节点定义核对" in results[0][1]
    assert "参数 prompt 的字段 1.text 不在节点类型 CLIPTextEncode 的输入中" in (
        results[0][1]
    )
