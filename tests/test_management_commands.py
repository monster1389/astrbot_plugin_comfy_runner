"""管理命令的用户可见行为。"""

import asyncio
import json
from pathlib import Path

from helpers import FakeComfyServer, FakeEvent, collect, make_plugin, run_tool

WORKFLOW = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
}

CONTRACT = {
    "version": 1,
    "name": "comfy_moss_t2i",
    "description": "使用 MOSS 工作流根据提示词生成图片",
    "model_visible": True,
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
    return {"admin_ids": ["9"], "whitelist_ids": ["2"]}


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


def test_list_command_shows_pairing(tmp_path, monkeypatch):
    """管理员执行 /comfy ls 展示工作流与契约的配对、可见性与校验状态。"""
    workflow_dir = write_workflow_dir(tmp_path)
    (workflow_dir / "plain.json").write_text("{}", encoding="utf-8")
    (workflow_dir / "moss.steps.json").write_text("{}", encoding="utf-8")
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    orphan = dict(CONTRACT, name="comfy_orphan", model_visible=False)
    (workflow_dir / "orphan.tool.json").write_text(
        json.dumps(orphan),
        encoding="utf-8",
    )
    results = collect(plugin.cmd_comfy_list(FakeEvent("/comfy ls", sender="9")))

    assert "1. ✅ moss.json -> comfy_moss_t2i [模型可见]" in results[0][1]
    assert "⚠️ plain.json 无配套契约" in results[0][1]
    assert (
        "❌ orphan.tool.json -> comfy_orphan [仅命令] 缺少工作流 orphan.json"
        in results[0][1]
    )
    assert "moss.steps.json" not in results[0][1]


def test_list_command_shows_user_workflows_without_paths(tmp_path, monkeypatch):
    """白名单普通用户执行 /comfy ls 只看可用工作流，不暴露文件与目录路径。"""
    workflow_dir = write_workflow_dir(tmp_path)
    (workflow_dir / "cmd_only.json").write_text(
        json.dumps(WORKFLOW),
        encoding="utf-8",
    )
    (workflow_dir / "cmd_only.tool.json").write_text(
        json.dumps(dict(CONTRACT, name="comfy_cmd_only", model_visible=False)),
        encoding="utf-8",
    )
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    results = collect(plugin.cmd_comfy_list(FakeEvent("/comfy ls", sender="5")))
    text = results[0][1]

    assert "1. comfy_cmd_only [仅命令]" in text
    assert "2. comfy_moss_t2i [模型可见]" in text
    assert "moss.json" not in text
    assert "目录:" not in text


def test_help_command_lists_enabled_workflows(tmp_path, monkeypatch):
    """/comfy help 显示已启用的工作流。"""
    write_workflow_dir(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    results = collect(plugin.cmd_comfy_help(FakeEvent("/comfy help", sender="9")))

    assert "可用工作流：comfy_moss_t2i" in results[0][1]
    assert "/comfy run" in results[0][1]
    assert "/comfy check" in results[0][1]
    assert "/comfy reload" in results[0][1]
    assert "/comfy save" not in results[0][1]


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
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    broken = json.loads(json.dumps(CONTRACT))
    broken.update(name="comfy_broken", model_visible=False)
    broken["inputs"]["prompt"]["target"] = {"node": "99", "field": "text"}
    (workflow_dir / "moss.tool.json").write_text(
        json.dumps(broken),
        encoding="utf-8",
    )

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


def write_workflow_only(tmp_path: Path) -> Path:
    """只写一份工作流，返回工作流目录。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "moss.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")
    return workflow_dir


def test_reload_command_requires_admin(tmp_path, monkeypatch):
    """/comfy reload 仅管理员可用。"""
    write_workflow_dir(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())

    results = collect(plugin.cmd_comfy_reload(FakeEvent("/comfy reload", sender="5")))

    assert "权限不足" in results[0][1]


def test_reload_command_registers_new_contract(tmp_path, monkeypatch):
    """新增契约后 /comfy reload 让工具与命令上下文立即生效。"""
    workflow_dir = write_workflow_only(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    assert plugin.function_tools == []

    (workflow_dir / "moss.tool.json").write_text(
        json.dumps(CONTRACT),
        encoding="utf-8",
    )
    results = collect(plugin.cmd_comfy_reload(FakeEvent("/comfy reload", sender="9")))

    assert "已重新加载工具契约，当前可用：comfy_moss_t2i" in results[0][1]
    assert [tool.name for tool in plugin.context.llm_tools] == [
        "comfy_list",
        "comfy_inspect",
        "comfy_execute",
    ]
    listed = run_tool(plugin, "comfy_list", FakeEvent("/comfy list", sender="9"))
    assert "comfy_moss_t2i" in listed
    help_text = collect(plugin.cmd_comfy_help(FakeEvent("/comfy help", sender="9")))
    assert "可用工作流：comfy_moss_t2i" in help_text[0][1]


def test_reload_command_removes_tools_when_contracts_disappear(tmp_path, monkeypatch):
    """删掉全部契约后 /comfy reload 移除三个工作流工具。"""
    workflow_dir = write_workflow_dir(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    asyncio.run(plugin.initialize())
    assert len(plugin.context.llm_tools) == 3

    (workflow_dir / "moss.tool.json").unlink()
    results = collect(plugin.cmd_comfy_reload(FakeEvent("/comfy reload", sender="9")))

    assert "当前可用：无" in results[0][1]
    assert plugin.function_tools == []
    assert plugin.context.llm_tools == []


def test_reload_command_keeps_state_when_contract_broken(tmp_path, monkeypatch):
    """契约损坏时 /comfy reload 报错且保留原运行状态。"""
    workflow_dir = write_workflow_dir(tmp_path)
    plugin, _ = make_plugin(tmp_path, monkeypatch, control=admin_overrides())
    (workflow_dir / "moss.tool.json").write_text("{", encoding="utf-8")

    results = collect(plugin.cmd_comfy_reload(FakeEvent("/comfy reload", sender="9")))

    assert "重新加载失败，保持原运行状态" in results[0][1]
    listed = run_tool(plugin, "comfy_list", FakeEvent("/comfy list", sender="9"))
    assert "comfy_moss_t2i" in listed
    assert [tool.name for tool in plugin.function_tools] == [
        "comfy_list",
        "comfy_inspect",
        "comfy_execute",
    ]


def test_private_chat_requires_whitelist_and_respects_scope(tmp_path, monkeypatch):
    """私聊默认受白名单控制，group:ID 不放行同名私聊用户。"""
    write_workflow_dir(tmp_path)
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={
            "admin_ids": ["9"],
            "whitelist_ids": ["group:100", "telegram:user:200"],
        },
    )

    denied = collect(
        plugin.cmd_comfy_help(
            FakeEvent("/comfy help", sender="100", group="", platform="aiocqhttp")
        )
    )
    assert "不在白名单中" in denied[0][1]
    assert "aiocqhttp:100" in denied[0][1]

    allowed = collect(
        plugin.cmd_comfy_help(
            FakeEvent("/comfy help", sender="200", group="", platform="telegram")
        )
    )
    assert "当前位置：私聊 telegram:200" in allowed[0][1]


def test_platform_scoped_admin_and_cooldown_isolation(tmp_path, monkeypatch):
    """管理员与冷却均按平台隔离，跨平台同数字 ID 互不影响。"""
    write_workflow_dir(tmp_path)
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={
            "admin_ids": ["telegram:9"],
            "whitelist_ids": ["2"],
            "cooldown_seconds": 60,
        },
    )

    qq_denied = collect(
        plugin.cmd_comfy_lock(
            FakeEvent("/comfy lock on", sender="9", group="2", platform="aiocqhttp")
        )
    )
    assert "权限不足" in qq_denied[0][1]

    tg_ok = collect(
        plugin.cmd_comfy_lock(
            FakeEvent("/comfy lock on", sender="9", group="2", platform="telegram")
        )
    )
    assert "已开启全局锁定" in tg_ok[0][1]

    ok_tg, _ = plugin.access.check_cooldown(
        FakeEvent(sender="10", group="2", platform="telegram")
    )
    ok_qq, _ = plugin.access.check_cooldown(
        FakeEvent(sender="10", group="2", platform="aiocqhttp")
    )
    again_tg, _ = plugin.access.check_cooldown(
        FakeEvent(sender="10", group="2", platform="telegram")
    )
    assert ok_tg is True
    assert ok_qq is True
    assert again_tg is False


def test_telegram_topic_whitelist_and_policy_fallback(tmp_path, monkeypatch):
    """Telegram 话题群继承整群白名单，/comfy policy 仅作用于当前话题。"""
    write_workflow_dir(tmp_path)
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={
            "admin_ids": ["telegram:9"],
            "whitelist_ids": ["telegram:group:-1001"],
            "default_group_policy": "lite",
        },
    )

    help_res = collect(
        plugin.cmd_comfy_help(
            FakeEvent("/comfy help", sender="7", group="-1001#42", platform="telegram")
        )
    )
    assert "当前位置：群聊 telegram:-1001#42" in help_res[0][1]

    collect(
        plugin.cmd_set_policy(
            FakeEvent(
                "/comfy policy full",
                sender="9",
                group="-1001#42",
                platform="telegram",
            )
        )
    )
    assert (
        plugin.access.policy_for(
            FakeEvent(sender="7", group="-1001#42", platform="telegram")
        )
        == "full"
    )
    assert (
        plugin.access.policy_for(
            FakeEvent(sender="7", group="-1001#99", platform="telegram")
        )
        == "lite"
    )
