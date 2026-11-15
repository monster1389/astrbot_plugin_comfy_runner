"""工作流执行：提交、后台交付、参数覆盖与错误告知。"""

import asyncio
import json
import re
from pathlib import Path

from helpers import (
    FakeComfyServer,
    FakeEvent,
    make_plugin,
    run_tool,
    sent_texts,
    tool_of,
)

WORKFLOW = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "原始提示词"}},
    "2": {
        "class_type": "KSampler",
        "inputs": {"seed": 7, "noise_seed": 11, "steps": 20},
    },
    "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}},
}

CONTRACT = {
    "version": 1,
    "name": "comfy_test_t2i",
    "description": "测试用工作流",
    "enabled": True,
    "inputs": {
        "prompt": {
            "type": "string",
            "description": "提示词",
            "required": True,
            "target": {"node": "1", "field": "text"},
        },
        "seed": {
            "type": "integer",
            "description": "种子",
            "target": {"node": "2", "field": "seed"},
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
            "3": {"images": [{"filename": "a.png", "subfolder": "s", "type": "output"}]}
        },
    }
}


def setup(
    tmp_path: Path,
    monkeypatch,
    execution: dict | None = None,
    control=None,
    contract: dict | None = None,
):
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
        execution=execution or {},
        control=control
        or {"admin_ids": ["9"], "whitelist_ids": ["2"], "cooldown_seconds": 0},
    )
    server = FakeComfyServer().install(monkeypatch)
    server.history_reply = SUCCESS
    return plugin, server


def contract_with(outputs: dict, inputs: dict | None = None) -> dict:
    """把基础契约的产物映射换成给定类型。"""
    contract = json.loads(json.dumps(CONTRACT))
    contract["outputs"] = outputs
    if inputs is not None:
        contract["inputs"] = inputs
    return contract


def execute(plugin, event=None, params=None) -> str:
    """对测试工作流执行一次，并等后台交付结束。"""
    return run_tool(
        plugin,
        "comfy_execute",
        event or FakeEvent(),
        workflow=CONTRACT["name"],
        arguments=params,
    )


def test_execute_returns_before_artifacts_are_sent(tmp_path, monkeypatch):
    """工具先返回提交结果，产物在之后的后台交付里发出。"""
    plugin, _ = setup(tmp_path, monkeypatch)
    event = FakeEvent(sender="9", group="2")

    async def scenario():
        tool = tool_of(plugin, "comfy_execute")
        summary = await tool.handler(
            event,
            workflow=CONTRACT["name"],
            arguments={"prompt": "一只猫"},
        )
        sent_before = len(event.sent)
        await asyncio.gather(*plugin.jobs.tasks)
        return summary, sent_before

    summary, sent_before = asyncio.run(scenario())

    assert sent_before == 0
    assert summary == (
        "已提交工作流 comfy_test_t2i，正在后台生成，完成后会自动发送到当前会话"
    )
    assert len(event.sent) == 1
    kind, components = event.sent[0]
    assert kind == "chain"
    assert [type(item).__name__ for item in components] == ["Image"]
    assert len(list((tmp_path / "plugin_data" / "output").glob("*.png"))) == 1


def test_summary_hides_internal_location(tmp_path, monkeypatch):
    """给模型的结果不含节点、路径或 ComfyUI 地址。"""
    plugin, _ = setup(tmp_path, monkeypatch)

    summary = execute(plugin, FakeEvent(sender="9", group="2"), {"prompt": "一只猫"})

    assert "http" not in summary
    assert str(tmp_path) not in summary
    assert "1.text" not in summary


def test_omitted_parameter_restores_workflow_value(tmp_path, monkeypatch):
    """连续调用后省略参数会回到原始工作流的值。"""
    plugin, server = setup(tmp_path, monkeypatch)

    execute(plugin, params={"prompt": "第一次", "seed": 5})
    execute(plugin, params={"prompt": "第二次", "seed": 9})
    execute(plugin, params={"prompt": "第三次"})

    assert [graph["2"]["inputs"]["seed"] for graph in server.submissions] == [5, 9, 7]
    assert [graph["1"]["inputs"]["text"] for graph in server.submissions] == [
        "第一次",
        "第二次",
        "第三次",
    ]


def test_unmapped_seed_and_workflow_file_untouched(tmp_path, monkeypatch):
    """未映射的 noise_seed 保持不变，原始工作流文件不被写回。"""
    plugin, server = setup(tmp_path, monkeypatch)
    path = tmp_path / "plugin_data" / "workflow" / "wf.json"
    before = path.read_bytes()

    execute(plugin, params={"prompt": "一次", "seed": 5})

    assert path.read_bytes() == before
    assert server.submissions[0]["2"]["inputs"]["noise_seed"] == 11
    assert server.submissions[0]["2"]["inputs"]["steps"] == 20


def test_missing_required_parameter_blocks_submission(tmp_path, monkeypatch):
    """缺少必填参数时把原因返回给模型，不提交工作流。"""
    plugin, server = setup(tmp_path, monkeypatch)

    summary = execute(plugin)

    assert summary == "❌ 缺少必填参数: prompt"
    assert server.submissions == []


def test_unknown_parameter_blocks_submission(tmp_path, monkeypatch):
    """出现未声明参数时把原因返回给模型，不提交工作流。"""
    plugin, server = setup(tmp_path, monkeypatch)

    summary = execute(plugin, params={"prompt": "一次", "style": "二次元"})

    assert summary == "❌ 出现未声明参数: style"
    assert server.submissions == []


def test_non_object_arguments_are_rejected(tmp_path, monkeypatch):
    """参数不是对象时给出明确错误。"""
    plugin, server = setup(tmp_path, monkeypatch)

    summary = execute(plugin, params="一次")

    assert "参数 arguments 必须是对象" in summary
    assert server.submissions == []


def test_unknown_workflow_is_reported(tmp_path, monkeypatch):
    """未知工作流不提交并提示可用名称。"""
    plugin, server = setup(tmp_path, monkeypatch)

    summary = run_tool(
        plugin,
        "comfy_execute",
        FakeEvent(sender="9", group="2"),
        workflow="comfy_nope",
        arguments={"prompt": "一次"},
    )

    assert "未知的工作流: comfy_nope" in summary
    assert "comfy_test_t2i" in summary
    assert server.submissions == []


def test_unlisted_group_cannot_execute(tmp_path, monkeypatch):
    """白名单外的群不提交工作流。"""
    plugin, server = setup(tmp_path, monkeypatch)

    summary = execute(plugin, FakeEvent(sender="5", group="8"), {"prompt": "一只猫"})

    assert "不在白名单中" in summary
    assert server.submissions == []


def test_sensitive_prompt_is_rejected(tmp_path, monkeypatch):
    """参数命中敏感词时不提交工作流。"""
    plugin, server = setup(
        tmp_path,
        monkeypatch,
        control={
            "admin_ids": ["9"],
            "whitelist_ids": ["2"],
            "default_group_policy": "full",
        },
    )

    summary = execute(plugin, FakeEvent(sender="5", group="2"), {"prompt": "blood"})

    assert "检测到敏感词" in summary
    assert server.submissions == []


def test_submit_error_is_reported(tmp_path, monkeypatch):
    """HTTP 200 但含提交错误时把原因返回给模型。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.prompt_response = (
        200,
        {
            "error": {"type": "prompt_no_outputs", "message": "Prompt has no outputs"},
            "node_errors": {},
        },
    )

    summary = execute(plugin, params={"prompt": "一次"})

    assert summary.startswith("❌")
    assert "提交失败" in summary
    assert "prompt_no_outputs" in summary
    assert server.history_queries == []


def test_node_errors_are_reported(tmp_path, monkeypatch):
    """node_errors 指出节点与原因。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.prompt_response = (
        200,
        {
            "node_errors": {
                "1": {"errors": [{"type": "bad_linked_input", "message": "输入是连线"}]}
            }
        },
    )

    summary = execute(plugin, params={"prompt": "一次"})

    assert "节点 1: bad_linked_input 输入是连线" in summary


def test_non_200_submit_is_reported(tmp_path, monkeypatch):
    """提交返回非 200 时按提交失败处理。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.prompt_response = (500, "boom")

    summary = execute(plugin, params={"prompt": "一次"})

    assert "提交失败: HTTP 500" in summary


def test_execution_failure_notifies_session(tmp_path, monkeypatch):
    """执行失败时由后台向会话发送原因。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.history_reply = {
        "p1": {
            "status": {
                "status_str": "error",
                "completed": True,
                "messages": [
                    [
                        "execution_error",
                        {"node_id": "2", "exception_message": "显存不足"},
                    ]
                ],
            },
            "outputs": {},
        }
    }
    event = FakeEvent(sender="9", group="2")

    execute(plugin, event, {"prompt": "一次"})

    assert sent_texts(event) == [
        "❌ 工作流 comfy_test_t2i 执行失败：执行失败: 2 显存不足"
    ]


def test_missing_declared_output_notifies_session(tmp_path, monkeypatch):
    """执行完成但声明的产物缺失时告知会话。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.history_reply = {
        "p1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"9": {"images": []}},
        }
    }
    event = FakeEvent(sender="9", group="2")

    execute(plugin, event, {"prompt": "一次"})

    assert sent_texts(event) == [
        "❌ 工作流 comfy_test_t2i 执行失败：产物 image 的输出节点 3 没有结果"
    ]


def test_download_failure_notifies_session(tmp_path, monkeypatch):
    """产物下载失败时告知会话。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.view_response = (404, "")
    event = FakeEvent(sender="9", group="2")

    execute(plugin, event, {"prompt": "一次"})

    texts = sent_texts(event)
    assert len(texts) == 1
    assert "下载 a.png 失败: HTTP 404" in texts[0]


def test_timeout_notifies_unknown_state_without_resubmitting(tmp_path, monkeypatch):
    """超时按执行状态未知告知会话，且不重新提交。"""
    plugin, server = setup(
        tmp_path,
        monkeypatch,
        execution={"timeout_seconds": 1, "poll_interval_seconds": 0.1},
    )
    server.history_reply = {}
    event = FakeEvent(sender="9", group="2")

    execute(plugin, event, {"prompt": "一次"})

    assert sent_texts(event) == ["⚠️ 工作流 comfy_test_t2i 等待超时，执行状态未知"]
    assert len(server.submissions) == 1


def test_video_artifact_is_delivered_as_video_message(tmp_path, monkeypatch):
    """视频产物下载落盘并以视频消息发送。"""
    plugin, server = setup(
        tmp_path,
        monkeypatch,
        contract=contract_with(
            {"video": {"type": "video", "target": {"node": "3", "field": "images"}}}
        ),
    )
    server.history_reply = {
        "p1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "3": {
                    "images": [
                        {
                            "filename": "MiniMax_H3_00006_.mp4",
                            "subfolder": "video",
                            "type": "output",
                        }
                    ]
                }
            },
        }
    }
    event = FakeEvent(sender="9", group="2")

    execute(plugin, event, {"prompt": "一次"})

    assert len(event.sent) == 1
    kind, components = event.sent[0]
    assert kind == "chain"
    assert [type(item).__name__ for item in components] == ["Video"]
    assert len(list((tmp_path / "plugin_data" / "output").glob("*.mp4"))) == 1


def test_audio_artifact_is_delivered_as_record_message(tmp_path, monkeypatch):
    """音频产物以语音消息发送。"""
    plugin, server = setup(
        tmp_path,
        monkeypatch,
        contract=contract_with(
            {"audio": {"type": "audio", "target": {"node": "3", "field": "audio"}}}
        ),
    )
    server.history_reply = {
        "p1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "3": {
                    "audio": [
                        {"filename": "voice.wav", "subfolder": "", "type": "output"}
                    ]
                }
            },
        }
    }
    event = FakeEvent(sender="9", group="2")

    execute(plugin, event, {"prompt": "一次"})

    assert len(event.sent) == 1
    _, components = event.sent[0]
    assert [type(item).__name__ for item in components] == ["Record"]


def test_file_artifact_keeps_original_filename(tmp_path, monkeypatch):
    """普通文件产物用原文件名作为文件消息名。"""
    plugin, server = setup(
        tmp_path,
        monkeypatch,
        contract=contract_with(
            {"file": {"type": "file", "target": {"node": "3", "field": "files"}}}
        ),
    )
    server.history_reply = {
        "p1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "3": {
                    "files": [
                        {"filename": "报告.pdf", "subfolder": "doc", "type": "output"}
                    ]
                }
            },
        }
    }
    event = FakeEvent(sender="9", group="2")

    execute(plugin, event, {"prompt": "一次"})

    _, components = event.sent[0]
    assert [type(item).__name__ for item in components] == ["File"]
    assert components[0].name == "报告.pdf"
    assert len(list((tmp_path / "plugin_data" / "output").glob("*.pdf"))) == 1


def test_text_artifact_is_sent_without_download(tmp_path, monkeypatch):
    """文本产物直接发送，不下载也不落盘。"""
    plugin, server = setup(
        tmp_path,
        monkeypatch,
        contract=contract_with(
            {"caption": {"type": "text", "target": {"node": "3", "field": "text"}}}
        ),
    )
    server.history_reply = {
        "p1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"3": {"text": ["一只猫坐在窗台上"]}},
        }
    }
    event = FakeEvent(sender="9", group="2")

    execute(plugin, event, {"prompt": "一次"})

    assert sent_texts(event) == ["一只猫坐在窗台上"]
    assert server.downloads == []
    assert list((tmp_path / "plugin_data" / "output").iterdir()) == []


def test_each_artifact_is_sent_as_its_own_message(tmp_path, monkeypatch):
    """同一次执行的多个产物各自单独发送。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.history_reply = {
        "p1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "3": {
                    "images": [
                        {"filename": "a.png", "subfolder": "", "type": "output"},
                        {"filename": "b.png", "subfolder": "", "type": "output"},
                    ]
                }
            },
        }
    }
    event = FakeEvent(sender="9", group="2")

    execute(plugin, event, {"prompt": "一次"})

    assert len(event.sent) == 2
    for kind, components in event.sent:
        assert kind == "chain"
        assert [type(item).__name__ for item in components] == ["Image"]


def test_text_artifact_rejects_non_string_entry(tmp_path, monkeypatch):
    """文本产物里出现非字符串条目时按产物缺失处理。"""
    plugin, server = setup(
        tmp_path,
        monkeypatch,
        contract=contract_with(
            {"caption": {"type": "text", "target": {"node": "3", "field": "text"}}}
        ),
    )
    server.history_reply = {
        "p1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"3": {"text": [{"filename": "a.txt"}]}},
        }
    }
    event = FakeEvent(sender="9", group="2")

    execute(plugin, event, {"prompt": "一次"})

    assert sent_texts(event) == [
        "❌ 工作流 comfy_test_t2i 执行失败：产物 caption 的第 1 项不是文本"
    ]


def test_concurrency_limit_rejects_without_submitting(tmp_path, monkeypatch):
    """并发已满时把拒绝原因返回给模型，不提交也不排队。"""
    plugin, server = setup(tmp_path, monkeypatch, execution={"max_concurrency": 1})
    assert plugin.gate.try_acquire()

    summary = execute(plugin, params={"prompt": "一次"})

    plugin.gate.release()
    assert summary == "⚠️ 并发已满（上限 1），请稍后重试"
    assert server.submissions == []
    assert plugin.gate.active == 0


def test_slot_is_released_after_timeout(tmp_path, monkeypatch):
    """后台超时后归还并发名额，下一次调用可以正常进入。"""
    plugin, server = setup(
        tmp_path,
        monkeypatch,
        execution={
            "max_concurrency": 1,
            "timeout_seconds": 1,
            "poll_interval_seconds": 0.1,
        },
    )
    server.history_reply = {}

    execute(plugin, params={"prompt": "一次"})

    assert plugin.gate.active == 0

    server.history_reply = SUCCESS
    assert execute(plugin, params={"prompt": "二次"}).startswith("已提交")
    assert plugin.gate.active == 0


def test_hidden_value_template_expands_date_on_submit(tmp_path, monkeypatch):
    """隐藏参数里的日期模板在提交副本时展开，不改工作流文件且不暴露给模型。"""
    contract = json.loads(json.dumps(CONTRACT))
    contract["inputs"]["prefix"] = {
        "type": "string",
        "description": "保存路径前缀",
        "visible": False,
        "target": {"node": "3", "field": "filename_prefix"},
        "value_template": "moss/%date:yyyy-MM-dd%/MOSS-%date:HHmmss%-%date%",
    }
    workflow = json.loads(json.dumps(WORKFLOW))
    workflow["3"]["inputs"]["filename_prefix"] = "moss/2026-11-14/MOSS-102348"

    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    wf_path = workflow_dir / "wf.json"
    wf_path.write_text(json.dumps(workflow), encoding="utf-8")
    (workflow_dir / "wf.tool.json").write_text(
        json.dumps(contract),
        encoding="utf-8",
    )
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={"admin_ids": ["9"], "whitelist_ids": ["2"], "cooldown_seconds": 0},
    )
    server = FakeComfyServer().install(monkeypatch)
    server.history_reply = SUCCESS

    inspected = run_tool(
        plugin,
        "comfy_inspect",
        FakeEvent("看下", sender="2"),
        workflow="comfy_test_t2i",
    )
    assert "prefix" not in inspected

    summary = execute(plugin, params={"prompt": "一只猫"})
    assert summary.startswith("已提交")

    submitted_prefix = server.submissions[0]["3"]["inputs"]["filename_prefix"]
    assert re.fullmatch(
        r"moss/\d{4}-\d{2}-\d{2}/MOSS-\d{6}-\d{4}-\d{2}-\d{2}",
        submitted_prefix,
    )
    saved_wf = json.loads(wf_path.read_text(encoding="utf-8"))
    assert saved_wf["3"]["inputs"]["filename_prefix"] == "moss/2026-11-14/MOSS-102348"
