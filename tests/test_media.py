"""附件与参数引用：图片、视频、语音、文件作为工作流输入的可见行为。"""

import json
import wave
from pathlib import Path

import pytest
from astrbot.api.message_components import File, Image, Record, Reply, Video
from helpers import FakeComfyServer, FakeEvent, make_plugin, run_tool

WORKFLOW = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "原始提示词"}},
    "2": {"class_type": "LoadImage", "inputs": {"image": "原图.png"}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}},
}

CONTRACT = {
    "version": 1,
    "name": "comfy_i2i",
    "description": "按参考图重绘",
    "enabled": True,
    "inputs": {
        "prompt": {
            "type": "string",
            "description": "提示词",
            "required": True,
            "target": {"node": "1", "field": "text"},
        },
        "reference": {
            "type": "image",
            "description": "参考图",
            "required": True,
            "target": {"node": "2", "field": "image"},
        },
    },
    "outputs": {
        "image": {"type": "image", "target": {"node": "3", "field": "images"}},
    },
}

SUCCESS = {
    "p1": {
        "status": {"status_str": "success", "completed": True},
        "outputs": {
            "3": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}
        },
    }
}


def setup(tmp_path: Path, monkeypatch, contract: dict | None = None):
    """写一对契约与工作流、装配插件并接管 HTTP。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "wf.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")
    (workflow_dir / "wf.tool.json").write_text(
        json.dumps(contract or CONTRACT),
        encoding="utf-8",
    )
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={
            "admin_ids": ["9"],
            "whitelist_group_ids": ["2"],
            "cooldown_seconds": 0,
        },
    )
    server = FakeComfyServer().install(monkeypatch)
    server.history_reply = SUCCESS
    return plugin, server


def image_file(tmp_path: Path, name: str = "ref.png", content: bytes = b"png-data"):
    """在临时目录里放一张图，返回图片组件。"""
    path = tmp_path / name
    path.write_bytes(content)
    return Image.fromFileSystem(str(path))


def execute(plugin, event, params: dict) -> str:
    """执行一次工作流，并等后台交付结束。"""
    return run_tool(
        plugin,
        "comfy_execute",
        event,
        workflow=CONTRACT["name"],
        arguments=params,
    )


def test_attachment_image_is_uploaded_and_mapped(tmp_path, monkeypatch):
    """引用本条消息的图片时，上传后把返回的名字写进目标字段。"""
    plugin, server = setup(tmp_path, monkeypatch)
    photo = image_file(tmp_path)
    event = FakeEvent(sender="9", group="2", components=[photo])

    execute(plugin, event, {"prompt": "赛博朋克", "reference": "attachment"})

    assert len(server.uploads) == 1
    filename, content = server.uploads[0]
    assert content == b"png-data"
    assert filename.endswith(".png")
    assert server.submissions[0]["2"]["inputs"]["image"] == filename


def test_attachment_index_picks_nth_image(tmp_path, monkeypatch):
    """attachment:2 取本条消息的第二张图片。"""
    plugin, server = setup(tmp_path, monkeypatch)
    first = image_file(tmp_path, "one.png", b"first")
    second = image_file(tmp_path, "two.png", b"second")
    event = FakeEvent(sender="9", group="2", components=[first, second])

    execute(plugin, event, {"prompt": "一次", "reference": "attachment:2"})

    assert server.uploads[0][1] == b"second"


def test_missing_attachment_blocks_submission(tmp_path, monkeypatch):
    """消息里没有图片时拒绝提交，并告诉模型该怎么补救。"""
    plugin, server = setup(tmp_path, monkeypatch)
    event = FakeEvent(sender="9", group="2")

    summary = execute(plugin, event, {"prompt": "一次", "reference": "attachment"})

    assert summary.startswith("⚠️")
    assert "没有第 1 个图片附件" in summary
    assert server.uploads == []
    assert server.submissions == []


def test_quoted_image_is_used_when_message_has_none(tmp_path, monkeypatch):
    """本条消息没有图片时，回退到引用消息里的图片。"""
    plugin, server = setup(tmp_path, monkeypatch)
    photo = image_file(tmp_path, "quoted.png", b"quoted-bytes")
    event = FakeEvent(
        sender="9",
        group="2",
        components=[Reply(id="1", chain=[photo])],
    )

    execute(plugin, event, {"prompt": "一次", "reference": "attachment"})

    assert server.uploads[0][1] == b"quoted-bytes"


def test_current_message_image_wins_over_quoted(tmp_path, monkeypatch):
    """本条消息的图片优先于引用消息里的图片。"""
    plugin, server = setup(tmp_path, monkeypatch)
    quoted = image_file(tmp_path, "quoted.png", b"quoted-bytes")
    current = image_file(tmp_path, "current.png", b"current-bytes")
    event = FakeEvent(
        sender="9",
        group="2",
        components=[Reply(id="1", chain=[quoted]), current],
    )

    execute(plugin, event, {"prompt": "一次", "reference": "attachment"})

    assert server.uploads[0][1] == b"current-bytes"


def test_local_path_reference_is_uploaded(tmp_path, monkeypatch):
    """绝对路径也能作为引用，方便模型回传它看到的附件路径。"""
    video_contract = json.loads(json.dumps(CONTRACT))
    video_contract["inputs"]["reference"] = {
        "type": "video",
        "description": "参考视频",
        "required": True,
        "target": {"node": "2", "field": "image"},
    }
    plugin, server = setup(tmp_path, monkeypatch, contract=video_contract)
    source = tmp_path / "seen-by-model.mp4"
    source.write_bytes(b"mp4-bytes")

    execute(
        plugin,
        FakeEvent(sender="9", group="2"),
        {"prompt": "一次", "reference": str(source)},
    )

    assert server.uploads[0][1] == b"mp4-bytes"
    assert server.uploads[0][0].endswith(".mp4")


def test_file_uri_reference_is_uploaded(tmp_path, monkeypatch):
    """file:// URI 也能作为引用。"""
    plugin, server = setup(tmp_path, monkeypatch)
    source = tmp_path / "note.txt"
    source.write_bytes(b"note-bytes")

    execute(
        plugin,
        FakeEvent(sender="9", group="2"),
        {"prompt": "一次", "reference": source.as_uri()},
    )

    assert server.uploads[0][1] == b"note-bytes"


def test_http_reference_is_downloaded_then_uploaded(tmp_path, monkeypatch):
    """链接引用先下载再上传。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.external_response = (200, b"remote-bytes")

    execute(
        plugin,
        FakeEvent(sender="9", group="2"),
        {"prompt": "一次", "reference": "http://example.com/ref.png"},
    )

    assert server.uploads[0][1] == b"remote-bytes"


def test_unresolvable_reference_is_rejected(tmp_path, monkeypatch):
    """无法解析的引用给出可用写法，不提交。"""
    plugin, server = setup(tmp_path, monkeypatch)

    summary = execute(
        plugin,
        FakeEvent(sender="9", group="2"),
        {"prompt": "一次", "reference": "ref.png"},
    )

    assert "无法解析的引用" in summary
    assert "attachment" in summary
    assert server.uploads == []
    assert server.submissions == []


def test_reference_without_suffix_is_rejected(tmp_path, monkeypatch):
    """引用没有扩展名时无法判断类型，直接拒绝。"""
    plugin, server = setup(tmp_path, monkeypatch)
    source = tmp_path / "no-suffix"
    source.write_bytes(b"bytes")

    summary = execute(
        plugin,
        FakeEvent(sender="9", group="2"),
        {"prompt": "一次", "reference": str(source)},
    )

    assert "需要带扩展名" in summary
    assert server.submissions == []


def test_upload_failure_is_reported_to_model(tmp_path, monkeypatch):
    """上传失败时把原因返回给模型，不提交工作流。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.upload_response = (500, "boom")

    summary = execute(
        plugin,
        FakeEvent(sender="9", group="2", components=[image_file(tmp_path)]),
        {"prompt": "一次", "reference": "attachment"},
    )

    assert "上传" in summary
    assert "HTTP 500" in summary
    assert server.submissions == []


@pytest.mark.parametrize(
    ("kind", "component_factory", "extension"),
    [
        pytest.param("image", lambda path: Image.fromFileSystem(str(path)), ".png"),
        pytest.param("video", lambda path: Video.fromFileSystem(str(path)), ".mp4"),
        pytest.param("audio", lambda path: Record.fromFileSystem(str(path)), ".wav"),
        pytest.param(
            "file",
            lambda path: File(name="报告.pdf", file=str(path)),
            ".pdf",
        ),
    ],
)
def test_each_attachment_kind_is_uploadable(
    tmp_path,
    monkeypatch,
    kind,
    component_factory,
    extension,
):
    """四类附件都能被对应类型的参数引用。"""
    contract = json.loads(json.dumps(CONTRACT))
    contract["inputs"]["reference"]["type"] = kind
    plugin, server = setup(tmp_path, monkeypatch, contract=contract)
    source = tmp_path / f"source{extension}"
    _write_source(source, b"payload")

    execute(
        plugin,
        FakeEvent(sender="9", group="2", components=[component_factory(source)]),
        {"prompt": "一次", "reference": "attachment"},
    )

    filename, content = server.uploads[0]
    assert filename.endswith(extension)
    assert content


def test_unreadable_attachment_is_reported_to_model(tmp_path, monkeypatch):
    """附件已不在本地时告诉模型读不到，不把异常抛给框架。"""
    plugin, server = setup(tmp_path, monkeypatch)
    photo = image_file(tmp_path, "gone.png", b"gone")
    event = FakeEvent(sender="9", group="2", components=[photo])
    (tmp_path / "gone.png").unlink()

    summary = execute(plugin, event, {"prompt": "一次", "reference": "attachment"})

    assert summary.startswith("⚠️")
    assert "附件" in summary
    assert server.submissions == []


def test_missing_reference_parameter_carries_value_hint(tmp_path, monkeypatch):
    """漏传文件类必填参数时，报错里带出可用写法。"""
    plugin, server = setup(tmp_path, monkeypatch)

    summary = execute(plugin, FakeEvent(sender="9", group="2"), {"prompt": "一次"})

    assert "缺少必填参数: reference" in summary
    assert "attachment" in summary
    assert "本地绝对路径" in summary
    assert server.submissions == []


def test_omitted_reference_keeps_workflow_value(tmp_path, monkeypatch):
    """可选的文件类参数省略时保持工作流里的原文件。"""
    optional = json.loads(json.dumps(CONTRACT))
    optional["inputs"]["reference"]["required"] = False
    plugin, server = setup(tmp_path, monkeypatch, contract=optional)

    execute(plugin, FakeEvent(sender="9", group="2"), {"prompt": "一次"})

    assert server.uploads == []
    assert server.submissions[0]["2"]["inputs"]["image"] == "原图.png"


def _write_source(path: Path, content: bytes) -> None:
    if path.suffix == ".wav":
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 400)
        return
    path.write_bytes(content)
