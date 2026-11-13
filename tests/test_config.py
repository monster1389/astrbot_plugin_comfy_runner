"""配置错误会阻止插件加载。"""

import pytest
from astrbot_plugin_comfy_runner.comfy_runner.config import ConfigError
from helpers import make_plugin


def test_invalid_config_prevents_plugin_load(tmp_path, monkeypatch):
    """配置类型不符时插件加载直接抛错。"""
    with pytest.raises(ConfigError):
        make_plugin(
            tmp_path,
            monkeypatch,
            control={"cooldown_seconds": "35"},
        )
