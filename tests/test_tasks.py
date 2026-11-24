"""任务账本、临时产物下载、延迟补发、串联引用、中断等待与取消。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.pipeline.process_stage import follow_up
from helpers import FakeComfyServer, FakeEvent, make_plugin, run_tool, tool_of

T2I_WORKFLOW = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "默认提示词"}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
}

T2I_CONTRACT = {
    "version": 1,
    "name": "comfy_t2i",
    "description": "文生图",
    "model_visible": True,
    "inputs": {
        "prompt": {
            "type": "string",
            "description": "提示词",
            "required": True,
            "target": {"node": "1", "field": "text"},
        },
    },
    "outputs": {
        "image": {"type": "image", "target": {"node": "3", "field": "images"}},
    },
}

I2V_WORKFLOW = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "default.png"}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "默认动作"}},
    "3": {"class_type": "SaveVideo", "inputs": {"images": ["2", 0]}},
}

I2V_CONTRACT = {
    "version": 1,
    "name": "comfy_i2v",
    "description": "图生视频",
    "model_visible": True,
    "inputs": {
        "prompt": {
            "type": "string",
            "description": "动作提示词",
            "required": True,
            "target": {"node": "2", "field": "text"},
        },
        "image": {
            "type": "image",
            "description": "首帧参考图",
            "required": True,
            "target": {"node": "1", "field": "image"},
        },
    },
    "outputs": {
        "video": {"type": "video", "target": {"node": "3", "field": "images"}},
    },
}

P1_SUCCESS = {
    "status": {"status_str": "success", "completed": True},
    "outputs": {
        "3": {
            "images": [
                {
                    "filename": "moss_00001_.png",
                    "subfolder": "moss/2026-11-17",
                    "type": "output",
                }
            ]
        }
    },
}


def setup_pair(
    tmp_path: Path,
    monkeypatch,
    execution: dict | None = None,
    control: dict | None = None,
):
    """装配包含 t2i 与 i2v 两个工作流的插件并接管 HTTP。"""
    workflow_dir = tmp_path / "plugin_data" / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "t2i.json").write_text(json.dumps(T2I_WORKFLOW), encoding="utf-8")
    (workflow_dir / "t2i.tool.json").write_text(
        json.dumps(T2I_CONTRACT),
        encoding="utf-8",
    )
    (workflow_dir / "i2v.json").write_text(json.dumps(I2V_WORKFLOW), encoding="utf-8")
    (workflow_dir / "i2v.tool.json").write_text(
        json.dumps(I2V_CONTRACT),
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
    server.history_by_id["p1"] = P1_SUCCESS
    return plugin, server


def test_deliver_false_skips_download_and_send_then_manual_deliver(
    tmp_path,
    monkeypatch,
):
    """deliver=false 只记账不下载也不发送，之后可通过 comfy_task 补发。"""
    plugin, server = setup_pair(tmp_path, monkeypatch)
    event = FakeEvent(sender="9", group="2")

    summary = run_tool(
        plugin,
        "comfy_execute",
        event,
        workflow="comfy_t2i",
        arguments={"prompt": "一只猫"},
        deliver=False,
    )

    assert "任务: p1" in summary
    assert server.downloads == []
    assert event.sent == []
    assert list((tmp_path / "plugin_data" / "temp").rglob("*")) == []

    status = json.loads(
        run_tool(plugin, "comfy_task", event, action="status", prompt_id="p1")
    )
    assert status["task"]["status"] == "succeeded"
    assert status["task"]["delivery_status"] == "skipped"
    assert status["task"]["artifacts"][0]["subfolder"] == "moss/2026-11-17"
    assert status["task"]["artifacts"][0]["filename"] == "moss_00001_.png"

    delivered = json.loads(
        run_tool(plugin, "comfy_task", event, action="deliver", prompt_id="p1")
    )
    assert delivered["delivery_status"] == "delivered"
    assert len(server.downloads) == 1
    assert len(event.sent) == 1
    assert list((tmp_path / "plugin_data" / "temp").rglob("*")) == []


def test_download_preserves_comfy_path_reuses_file_and_clears_on_terminate(
    tmp_path,
    monkeypatch,
):
    """按 ComfyUI 路径落到 temp/ 并复用，插件卸载时统一清空。"""
    plugin, server = setup_pair(tmp_path, monkeypatch)
    event = FakeEvent(sender="9", group="2")

    run_tool(
        plugin,
        "comfy_execute",
        event,
        workflow="comfy_t2i",
        arguments={"prompt": "一只猫"},
        deliver=False,
    )
    first = json.loads(
        run_tool(plugin, "comfy_task", event, action="download", prompt_id="p1")
    )
    saved_path = Path(first["artifacts"][0]["path"])

    assert saved_path.is_file()
    assert saved_path.parts[-3:] == ("moss", "2026-11-17", "moss_00001_.png")
    assert "temp" in saved_path.parts
    assert len(server.downloads) == 1

    second = json.loads(
        run_tool(plugin, "comfy_task", event, action="download", prompt_id="p1")
    )
    assert Path(second["artifacts"][0]["path"]) == saved_path
    assert len(server.downloads) == 1

    asyncio.run(plugin.terminate())
    assert not saved_path.exists()
    assert list((tmp_path / "plugin_data" / "temp").rglob("*")) == []


def test_task_ledger_persists_across_comfy_and_plugin_restart(
    tmp_path,
    monkeypatch,
):
    """ComfyUI 重启清空历史且 AstrBot 重启后，仍可凭 tasks.db 下载与补发。"""
    plugin, server = setup_pair(tmp_path, monkeypatch)
    event = FakeEvent(sender="9", group="2")

    run_tool(
        plugin,
        "comfy_execute",
        event,
        workflow="comfy_t2i",
        arguments={"prompt": "持久化测试"},
        deliver=False,
    )
    asyncio.run(plugin.terminate())

    restarted, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control={"admin_ids": ["9"], "whitelist_ids": ["2"], "cooldown_seconds": 0},
    )
    server2 = FakeComfyServer().install(monkeypatch)
    server2.history_reply = {}
    server2.view_by_filename["moss_00001_.png"] = b"after-comfy-restart"

    status = json.loads(
        run_tool(restarted, "comfy_task", event, action="status", prompt_id="p1")
    )
    assert status["task"]["status"] == "succeeded"
    assert status["task"]["arguments"]["prompt"] == "持久化测试"

    dl = json.loads(
        run_tool(restarted, "comfy_task", event, action="download", prompt_id="p1")
    )
    assert Path(dl["artifacts"][0]["path"]).read_bytes() == b"after-comfy-restart"

    sent_res = json.loads(
        run_tool(restarted, "comfy_task", event, action="deliver", prompt_id="p1")
    )
    assert sent_res["delivery_status"] == "delivered"
    assert len(event.sent) == 1
    assert server2.history_queries == []


def test_task_reference_chains_running_upstream_without_blocking(
    tmp_path,
    monkeypatch,
):
    """引用尚未完成的上游任务时立即返回，后台等待完成后自动上传并串联执行。"""
    plugin, server = setup_pair(
        tmp_path,
        monkeypatch,
        execution={"max_concurrency": 2, "poll_interval_seconds": 0.05},
    )
    server.prompt_queue = [
        (200, {"prompt_id": "p1", "node_errors": {}}),
        (200, {"prompt_id": "p2", "node_errors": {}}),
    ]
    server.history_by_id = {
        "p2": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "3": {
                    "images": [
                        {"filename": "clip.mp4", "subfolder": "vid", "type": "output"}
                    ]
                }
            },
        }
    }
    server.history_queue = [{}, {"p1": P1_SUCCESS}]
    server.history_reply = {"p1": P1_SUCCESS}
    server.view_by_filename["moss_00001_.png"] = b"upstream-image-bytes"
    server.view_by_filename["clip.mp4"] = b"downstream-video-bytes"
    event = FakeEvent(sender="9", group="2")

    async def scenario():
        exec_tool = tool_of(plugin, "comfy_execute")
        first_res = await exec_tool.handler(
            event,
            workflow="comfy_t2i",
            arguments={"prompt": "首帧"},
            deliver=False,
        )
        second_res = await exec_tool.handler(
            event,
            workflow="comfy_i2v",
            arguments={"prompt": "动起来", "image": "task:p1"},
            deliver=True,
        )
        await asyncio.gather(*list(plugin.jobs.tasks))
        return first_res, second_res

    first_res, second_res = asyncio.run(scenario())
    assert "任务: p1" in first_res
    assert "正在等待前置任务完成后生成并发送到当前会话" in second_res

    alias_id = second_res.split("任务: ", 1)[1].split("）", 1)[0]
    status = json.loads(
        run_tool(plugin, "comfy_task", event, action="status", prompt_id=alias_id)
    )
    assert status["task"]["prompt_id"] == "p2"
    assert status["task"]["status"] == "succeeded"
    assert status["task"]["delivery_status"] == "delivered"
    assert server.uploads[0][1] == b"upstream-image-bytes"
    assert len(event.sent) == 1
    assert [type(c).__name__ for c in event.sent[0][1]] == ["Video"]


def test_queue_pending_exempt_from_timeout_and_late_completion_heals(
    tmp_path,
    monkeypatch,
):
    """ComfyUI 排队期不计入执行超时，超时后已完成的任务查询时可自动自愈。"""
    plugin, server = setup_pair(
        tmp_path,
        monkeypatch,
        execution={"timeout_seconds": 1, "poll_interval_seconds": 0.25},
    )
    event = FakeEvent(sender="9", group="2")
    server.history_by_id = {}
    server.history_queue = [{}, {}, {}, {}, {}, {"p1": P1_SUCCESS}]
    server.queue_queue = [
        {"queue_running": [], "queue_pending": [[1, "p1", {}, {}, []]]}
        for _ in range(5)
    ]

    summary = run_tool(
        plugin,
        "comfy_execute",
        event,
        workflow="comfy_t2i",
        arguments={"prompt": "排队测试"},
    )
    assert "任务: p1" in summary
    assert len(event.sent) == 1
    assert event.sent[0][0] == "chain"

    event2 = FakeEvent(sender="9", group="2")
    server.prompt_response = (200, {"prompt_id": "p2", "node_errors": {}})
    server.history_by_id = {}
    server.history_queue = []
    server.history_reply = {}
    server.queue_queue = []
    server.queue_reply = {"queue_running": [], "queue_pending": []}
    run_tool(
        plugin,
        "comfy_execute",
        event2,
        workflow="comfy_t2i",
        arguments={"prompt": "迟到完成"},
    )
    unknown_view = json.loads(
        run_tool(plugin, "comfy_task", event2, action="status", prompt_id="p2")
    )
    assert unknown_view["task"]["status"] == "unknown"

    server.history_by_id["p2"] = P1_SUCCESS
    healed_view = json.loads(
        run_tool(plugin, "comfy_task", event2, action="status", prompt_id="p2")
    )
    assert healed_view["task"]["status"] == "succeeded"
    assert healed_view["task"]["artifacts"][0]["filename"] == "moss_00001_.png"


def test_delivery_retries_and_preserves_succeeded_status_on_failure(
    tmp_path,
    monkeypatch,
):
    """发送超时会自动重试；彻底失败时保留执行成功状态并支持后续补发。"""
    from astrbot_plugin_comfy_runner.comfy_runner import delivery

    monkeypatch.setattr(delivery, "SEND_RETRY_DELAY_SECONDS", 0.0)
    plugin, server = setup_pair(tmp_path, monkeypatch)

    event_retry = FakeEvent(sender="9", group="2")
    event_retry.send_errors = [TimeoutError("Timed out")]
    run_tool(
        plugin,
        "comfy_execute",
        event_retry,
        workflow="comfy_t2i",
        arguments={"prompt": "重试一次成功"},
    )
    status_ok = json.loads(
        run_tool(plugin, "comfy_task", event_retry, action="status", prompt_id="p1")
    )
    assert status_ok["task"]["delivery_status"] == "delivered"
    assert len(event_retry.sent) == 1

    event_fail = FakeEvent(sender="9", group="2")
    event_fail.send_errors = [
        TimeoutError("Timed out 1"),
        TimeoutError("Timed out 2"),
        TimeoutError("Timed out 3"),
    ]
    run_tool(
        plugin,
        "comfy_execute",
        event_fail,
        workflow="comfy_t2i",
        arguments={"prompt": "全失败"},
    )
    status_fail = json.loads(
        run_tool(plugin, "comfy_task", event_fail, action="status", prompt_id="p1")
    )
    assert status_fail["task"]["status"] == "succeeded"
    assert status_fail["task"]["delivery_status"] == "failed"

    resend = json.loads(
        run_tool(plugin, "comfy_task", event_fail, action="deliver", prompt_id="p1")
    )
    assert resend["delivery_status"] == "delivered"


def test_wait_interrupts_when_follow_up_arrives(tmp_path, monkeypatch):
    """等待期间收到用户新消息时在一个周期内立即中断返回。"""
    plugin, server = setup_pair(
        tmp_path,
        monkeypatch,
        execution={"timeout_seconds": 30, "poll_interval_seconds": 0.1},
    )
    server.history_by_id = {}
    server.history_reply = {}
    event = FakeEvent(sender="9", group="2")
    event.unified_msg_origin = "tg:wait:1"

    runner = SimpleNamespace(
        run_context=SimpleNamespace(context=SimpleNamespace(event=event)),
        request_stop=lambda: None,
        follow_up=lambda message_text: SimpleNamespace(
            resolved=asyncio.Event(),
            consumed=False,
        ),
    )
    follow_up.register_active_runner(event.unified_msg_origin, runner)

    follow_up_event = FakeEvent("先停一下", sender="9", group="2")
    follow_up_event.unified_msg_origin = event.unified_msg_origin

    async def run_interrupt_case():
        try:
            await tool_of(plugin, "comfy_execute").handler(
                event,
                workflow="comfy_t2i",
                arguments={"prompt": "等待中断"},
            )
            wait_task = asyncio.create_task(
                tool_of(plugin, "comfy_task").handler(
                    event,
                    action="wait",
                    prompt_id="p1",
                )
            )
            await asyncio.sleep(0.05)
            capture = follow_up.try_capture_follow_up(follow_up_event)
            payload = json.loads(await asyncio.wait_for(wait_task, timeout=2.0))
            if capture is not None:
                capture.monitor_task.cancel()
                await asyncio.gather(capture.monitor_task, return_exceptions=True)
            return payload
        finally:
            follow_up.unregister_active_runner(event.unified_msg_origin, runner)
            await plugin.terminate()

    result = asyncio.run(run_interrupt_case())
    assert result["interrupted"] is True
    assert "新消息" in result["reason"]
    assert result["tasks"][0]["prompt_id"] == "p1"


def test_cancel_pending_running_cascade_and_command_permission(
    tmp_path,
    monkeypatch,
):
    """取消排队与执行中任务会释放并发名额并级联取消下游，命令取消校验权限。"""
    plugin, server = setup_pair(
        tmp_path,
        monkeypatch,
        execution={"max_concurrency": 2, "poll_interval_seconds": 0.05},
        control={"admin_ids": ["9"], "whitelist_ids": ["2"], "cooldown_seconds": 0},
    )
    server.history_by_id = {}
    server.history_reply = {}
    server.queue_reply = {
        "queue_running": [[1, "p_run", {}, {}, []]],
        "queue_pending": [[2, "p_pend", {}, {}, []]],
    }
    server.prompt_queue = [
        (200, {"prompt_id": "p_run", "node_errors": {}}),
    ]
    owner = FakeEvent(sender="5", group="2")
    other = FakeEvent(sender="6", group="2")

    async def scenario():
        try:
            await tool_of(plugin, "comfy_execute").handler(
                owner,
                workflow="comfy_t2i",
                arguments={"prompt": "运行中"},
            )
            await tool_of(plugin, "comfy_execute").handler(
                owner,
                workflow="comfy_i2v",
                arguments={"prompt": "级联", "image": "task:p_run"},
            )
            assert plugin.gate.active == 2

            denied = [
                item
                async for item in plugin.cmd_comfy_cancel(
                    FakeEvent("/comfy cancel p_run", sender="6", group="2")
                )
            ]
            assert "权限不足" in denied[0][1]

            cancelled = json.loads(
                await tool_of(plugin, "comfy_task").handler(
                    owner,
                    action="cancel",
                    prompt_id="p_run",
                )
            )
            assert cancelled["concurrency"]["active"] == 0
            assert plugin.gate.active == 0
            assert server.interrupts == [{"prompt_id": "p_run"}]

            server.prompt_queue = [
                (200, {"prompt_id": "p_pend", "node_errors": {}}),
            ]
            await tool_of(plugin, "comfy_execute").handler(
                other,
                workflow="comfy_t2i",
                arguments={"prompt": "排队中"},
            )
            cmd_ok = [
                item
                async for item in plugin.cmd_comfy_cancel(
                    FakeEvent("/comfy cancel p_pend", sender="9", group="2")
                )
            ]
            assert "已取消" in cmd_ok[0][1]
            assert server.queue_deletions == [["p_pend"]]
            assert plugin.gate.active == 0
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
