"""工作流工具执行：参数覆盖、错误分类与并发上限。"""

import asyncio
import json
from pathlib import Path

import pytest
from astrbot_plugin_comfy_runner.comfy_runner.comfy import (
    ComfyError,
    DownloadError,
    ExecutionFailed,
    ExecutionTimeout,
    OutputError,
    SubmitError,
)
from astrbot_plugin_comfy_runner.comfy_runner.contract import ContractError
from helpers import FakeComfyServer, FakeEvent, make_plugin

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


def setup(tmp_path: Path, monkeypatch, execution: dict | None = None):
    """写一对契约与工作流、装配插件并接管 HTTP。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "wf.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")
    (workflow_dir / "wf.tool.json").write_text(
        json.dumps(CONTRACT),
        encoding="utf-8",
    )
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        execution=execution or {},
        control={"admin_ids": ["9"], "whitelist_group_ids": ["2"]},
    )
    server = FakeComfyServer().install(monkeypatch)
    server.history_reply = SUCCESS
    return plugin, server


def run(plugin, **arguments):
    """按 AstrBot 的调用方式执行一次工具。"""
    handler = plugin.function_tools[0].handler
    return asyncio.run(handler(FakeEvent(), **arguments))


def test_omitted_parameter_restores_workflow_value(tmp_path, monkeypatch):
    """连续调用后省略参数会回到原始工作流的值。"""
    plugin, server = setup(tmp_path, monkeypatch)

    run(plugin, prompt="第一次", seed=5)
    run(plugin, prompt="第二次", seed=9)
    run(plugin, prompt="第三次")

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

    run(plugin, prompt="一次", seed=5)

    assert path.read_bytes() == before
    assert server.submissions[0]["2"]["inputs"]["noise_seed"] == 11
    assert server.submissions[0]["2"]["inputs"]["steps"] == 20


def test_missing_required_parameter_blocks_submission(tmp_path, monkeypatch):
    """缺少必填参数时不提交工作流。"""
    plugin, server = setup(tmp_path, monkeypatch)

    with pytest.raises(ContractError, match="缺少必填参数: prompt"):
        run(plugin)

    assert server.submissions == []


def test_unknown_parameter_blocks_submission(tmp_path, monkeypatch):
    """出现未声明参数时不提交工作流。"""
    plugin, server = setup(tmp_path, monkeypatch)

    with pytest.raises(ContractError, match="出现未声明参数: style"):
        run(plugin, prompt="一次", style="二次元")

    assert server.submissions == []


def test_success_returns_summary_and_downloads_artifact(tmp_path, monkeypatch):
    """执行成功返回产物数量并下载声明的正式产物。"""
    plugin, server = setup(tmp_path, monkeypatch)

    summary = run(plugin, prompt="一次")

    assert summary == "已生成 1 个产物"
    assert len(server.downloads) == 1


def test_submit_error_is_reported(tmp_path, monkeypatch):
    """HTTP 200 但含提交错误时按提交失败处理。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.prompt_response = (
        200,
        {
            "error": {"type": "prompt_no_outputs", "message": "Prompt has no outputs"},
            "node_errors": {},
        },
    )

    with pytest.raises(SubmitError, match="prompt_no_outputs"):
        run(plugin, prompt="一次")

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

    with pytest.raises(SubmitError, match="节点 1: bad_linked_input 输入是连线"):
        run(plugin, prompt="一次")


def test_non_200_submit_is_reported(tmp_path, monkeypatch):
    """提交返回非 200 时按提交失败处理。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.prompt_response = (500, "boom")

    with pytest.raises(SubmitError, match="HTTP 500"):
        run(plugin, prompt="一次")


def test_execution_failure_is_reported(tmp_path, monkeypatch):
    """执行状态为 error 时报告节点与原因。"""
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

    with pytest.raises(ExecutionFailed, match="2 显存不足"):
        run(plugin, prompt="一次")


def test_missing_declared_output_is_reported(tmp_path, monkeypatch):
    """执行完成但没有声明的产物时按结果不完整处理。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.history_reply = {
        "p1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"9": {"images": []}},
        }
    }

    with pytest.raises(OutputError, match="产物 image 的输出节点 3 没有结果"):
        run(plugin, prompt="一次")


def test_download_failure_is_reported(tmp_path, monkeypatch):
    """产物下载失败单独报告。"""
    plugin, server = setup(tmp_path, monkeypatch)
    server.view_response = (404, "")

    with pytest.raises(DownloadError, match="HTTP 404"):
        run(plugin, prompt="一次")


def test_timeout_does_not_resubmit(tmp_path, monkeypatch):
    """超时结束本次调用，且不重新提交工作流。"""
    plugin, server = setup(
        tmp_path,
        monkeypatch,
        execution={"timeout_seconds": 1, "poll_interval_seconds": 0.1},
    )
    server.history_reply = {}

    with pytest.raises(ExecutionTimeout):
        run(plugin, prompt="一次")

    assert len(server.submissions) == 1


def test_concurrency_limit_rejects_without_queueing(tmp_path, monkeypatch):
    """并发已满时直接拒绝，不提交也不排队。"""
    plugin, server = setup(tmp_path, monkeypatch, execution={"max_concurrency": 1})
    assert plugin.gate.try_acquire()

    with pytest.raises(ComfyError, match="并发已满（上限 1）"):
        run(plugin, prompt="一次")

    plugin.gate.release()
    assert server.submissions == []
    assert plugin.gate.active == 0


def test_slot_is_released_after_timeout(tmp_path, monkeypatch):
    """超时后归还并发名额，下一次调用可以正常进入。"""
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

    with pytest.raises(ExecutionTimeout):
        run(plugin, prompt="一次")

    assert plugin.gate.active == 0

    server.history_reply = SUCCESS
    assert run(plugin, prompt="二次") == "已生成 1 个产物"
