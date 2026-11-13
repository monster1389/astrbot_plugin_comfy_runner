"""配置解析的边界行为。"""

import pytest
from helpers import make_config

from comfy_runner.config import Config, ConfigError


def test_missing_section_raises(tmp_path):
    """缺少整段配置时直接抛。"""
    with pytest.raises(ConfigError):
        Config.from_mapping({"server_address": "x"}, tmp_path)


def test_wrong_scalar_type_raises(tmp_path):
    """标量类型不匹配时直接抛。"""
    with pytest.raises(ConfigError):
        make_config(tmp_path, control={"cooldown_seconds": "35"})


def test_unknown_policy_raises(tmp_path):
    """违禁级别不在允许集合时直接抛。"""
    with pytest.raises(ConfigError):
        make_config(tmp_path, control={"default_group_policy": "strict"})


def test_paths_derive_from_data_dir(tmp_path):
    """三个路径都从数据目录派生。"""
    cfg = make_config(tmp_path)
    assert cfg.workflow_dir == tmp_path / "workflow"
    assert cfg.output_dir == tmp_path / "output"
    assert cfg.sensitive_words_path == tmp_path / "sensitive_words.json"
