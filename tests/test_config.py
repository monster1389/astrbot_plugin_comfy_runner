"""配置解析与加载前置条件。"""

import json

import pytest
from astrbot_plugin_comfy_runner.comfy_runner.config import ConfigError
from helpers import make_plugin


@pytest.mark.parametrize(
    "execution",
    [
        {"max_concurrency": 0},
        {"max_concurrency": "2"},
        {"timeout_seconds": 0},
        {"poll_interval_seconds": 0},
        {"poll_interval_seconds": "1"},
    ],
)
def test_invalid_execution_settings_prevent_load(tmp_path, monkeypatch, execution):
    """执行限制非法时插件加载直接抛错。"""
    with pytest.raises(ConfigError):
        make_plugin(tmp_path, monkeypatch, execution=execution)


def test_invalid_control_settings_prevent_load(tmp_path, monkeypatch):
    """访问控制配置类型不符时插件加载直接抛错。"""
    with pytest.raises(ConfigError):
        make_plugin(tmp_path, monkeypatch, control={"cooldown_seconds": "35"})


def test_unknown_policy_prevents_load(tmp_path, monkeypatch):
    """违禁级别不在允许集合内时插件加载直接抛错。"""
    with pytest.raises(ConfigError):
        make_plugin(tmp_path, monkeypatch, control={"default_group_policy": "max"})


def test_legacy_config_sections_do_not_block_load(tmp_path, monkeypatch):
    """升级后配置里残留 workflow_settings / llm_settings 也能正常加载。"""
    make_plugin(tmp_path, monkeypatch, control={"admin_ids": ["9"]})
    config_path = tmp_path / "config.json"
    data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    data["workflow_settings"] = {"json_file": "old.json", "bindings": "{}"}
    data["llm_settings"] = {"multi_image_mode": True}
    config_path.write_text(json.dumps(data), encoding="utf-8-sig")

    plugin, _ = make_plugin(tmp_path, monkeypatch, control={"admin_ids": ["9"]})

    assert plugin.cmd_ctx.tool_names == ()
