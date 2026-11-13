"""出图与多图发送的用户可见行为。"""

import asyncio

from astrbot.api.message_components import Plain
from helpers import FakeAPI, FakeEvent, FakeResult, collect, make_ctx

from comfy_runner.commands import (
    _ComfyImageMarker,
    paint,
    stage_build_chain,
    stage_send_images,
)


def test_paint_returns_forward_node(tmp_path):
    """/画图 出图后返回合并转发节点。"""
    api = FakeAPI()
    ctx = make_ctx(tmp_path, api=api)
    event = FakeEvent("/画图 one girl, smile")

    results = collect(paint(ctx, event, direct_send=False))

    assert len(results) == 1
    kind, components = results[0]
    assert kind == "chain"
    assert type(components[0]).__name__ == "Node"
    assert api.calls == ["one girl, smile"]
    assert len(list(tmp_path.glob("*.png"))) == 1


def test_paint_rejects_empty_prompt(tmp_path):
    """没有提示词时不调用客户端。"""
    ctx = make_ctx(tmp_path)
    results = collect(paint(ctx, FakeEvent("/画图"), direct_send=False))
    assert results[0][0] == "text"
    assert "/画图" in results[0][1]
    assert ctx.api.calls == []


def test_paint_rejects_sensitive_prompt(tmp_path):
    """普通用户命中敏感词时拒绝出图。"""
    ctx = make_ctx(tmp_path)
    ctx.access.lexicon = {"legacy_lite": ["blood"]}
    ctx.access.patterns = ctx.access._build_patterns()
    ctx.access.set_policy("2", "full")

    event = FakeEvent("/画图 blood, smile", sender="5")
    results = collect(paint(ctx, event, direct_send=False))

    assert results[0][0] == "text"
    assert "敏感词" in results[0][1]
    assert ctx.api.calls == []


def test_multi_image_chain_and_send_order(tmp_path):
    """多图模式按文字段、图片交替发送。"""
    api = FakeAPI()
    ctx = make_ctx(tmp_path, api=api, llm_settings={"multi_image_mode": True})
    event = FakeEvent()
    event._comfy_segments = [
        {"type": "text", "content": "第一段"},
        {"type": "prompt", "content": "one girl, smile", "lora_selections": []},
        {"type": "text", "content": "第二段"},
        {"type": "prompt", "content": "blue sky, clouds", "lora_selections": []},
    ]
    event.set_result(FakeResult([Plain("原始")]))

    asyncio.run(stage_build_chain(ctx, event))
    chain = event.get_result().chain

    assert type(chain[0]).__name__ == "Plain"
    assert isinstance(chain[1], _ComfyImageMarker)
    assert type(chain[2]).__name__ == "Plain"
    assert isinstance(chain[3], _ComfyImageMarker)

    asyncio.run(stage_send_images(ctx, event))

    assert [kind for kind, _ in event.sent] == ["chain", "chain", "chain", "chain"]
    assert api.calls == ["one girl, smile", "blue sky, clouds"]
    assert event.get_result().chain == []
