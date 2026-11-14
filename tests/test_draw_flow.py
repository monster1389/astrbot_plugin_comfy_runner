"""出图与多图发送的用户可见行为。"""

from pathlib import Path

from helpers import FakeComfyServer, FakeEvent, collect, make_plugin

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
