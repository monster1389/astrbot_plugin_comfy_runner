"""手动命令交互向导：不经过模型的多轮工作流参数收集与执行行为。"""

import asyncio
import json
from pathlib import Path

from astrbot.api.message_components import Image, Reply
from astrbot.core.utils.session_waiter import FILTERS, USER_SESSIONS, SessionWaiter
from helpers import (
    EXTERNAL_URL_PREFIX,
    FakeComfyServer,
    FakeEvent,
    make_plugin,
    sent_texts,
)

WORKFLOW = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "2": {
        "class_type": "KSampler",
        "inputs": {"seed": 42, "ratio": "1:1", "hires": False},
    },
    "3": {"class_type": "LoadImage", "inputs": {"image": "orig.png"}},
    "4": {"class_type": "LoadDoc", "inputs": {"path": "orig.txt"}},
    "5": {
        "class_type": "SaveImage",
        "inputs": {"images": ["2", 0], "filename_prefix": "out/static"},
    },
}

CONTRACT = {
    "version": 1,
    "name": "comfy_cmd_demo",
    "description": "命令测试工作流",
    "model_visible": False,
    "inputs": {
        "prompt": {
            "type": "string",
            "description": "提示词",
            "required": True,
            "minLength": 2,
            "target": {"node": "1", "field": "text"},
        },
        "ratio": {
            "type": "string",
            "description": "画幅",
            "required": True,
            "enum": ["1:1", "16:9"],
            "target": {"node": "2", "field": "ratio"},
        },
        "hires": {
            "type": "boolean",
            "description": "高清修复",
            "required": False,
            "target": {"node": "2", "field": "hires"},
        },
        "seed": {
            "type": "integer",
            "description": "随机种子",
            "required": False,
            "minimum": 0,
            "target": {"node": "2", "field": "seed"},
        },
        "prefix": {
            "type": "string",
            "description": "输出前缀",
            "visible": False,
            "value_template": "cmd/%date:yyyy%",
            "target": {"node": "5", "field": "filename_prefix"},
        },
    },
    "outputs": {
        "image": {"type": "image", "target": {"node": "5", "field": "images"}},
    },
}

HISTORY = {
    "p1": {
        "status": {"status_str": "success", "completed": True},
        "outputs": {
            "5": {
                "images": [{"filename": "res.png", "subfolder": "", "type": "output"}]
            }
        },
    }
}


def setup_workflows(
    tmp_path: Path,
    monkeypatch,
    contracts: dict[str, dict] | None = None,
    control: dict | None = None,
    sensitive_words: list[str] | None = None,
):
    """写入工作流与契约、装配插件并接管 ComfyUI HTTP。"""
    data_dir = tmp_path / "plugin_data"
    workflow_dir = data_dir / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    if sensitive_words is not None:
        (data_dir / "sensitive_words.json").write_text(
            json.dumps({"legacy_lite": sensitive_words}, ensure_ascii=False),
            encoding="utf-8",
        )
    items = contracts or {"demo": CONTRACT}
    for stem, contract in items.items():
        (workflow_dir / f"{stem}.json").write_text(
            json.dumps(WORKFLOW),
            encoding="utf-8",
        )
        (workflow_dir / f"{stem}.tool.json").write_text(
            json.dumps(contract),
            encoding="utf-8",
        )
    plugin, _ = make_plugin(
        tmp_path,
        monkeypatch,
        control=control
        or {"admin_ids": ["9"], "whitelist_ids": ["2"], "cooldown_seconds": 0},
    )
    server = FakeComfyServer().install(monkeypatch)
    server.history_reply = HISTORY
    return plugin, server


async def dispatch_reply(event: FakeEvent) -> bool:
    """模拟 AstrBot 管线把后续消息投递给匹配的 SessionWaiter。"""
    for session_filter in tuple(FILTERS):
        sid = session_filter.filter(event)
        session = USER_SESSIONS.get(sid)
        if session and not session.session_controller.future.done():
            await SessionWaiter.trigger(sid, event)
            return True
    return False


async def run_cmd(plugin, event: FakeEvent) -> list:
    """消费插件命令异步生成器并收集结果。"""
    return [item async for item in plugin.cmd_comfy_run(event)]


def test_command_only_workflow_runs_multi_turn_without_llm_tools(
    tmp_path,
    monkeypatch,
):
    """仅命令契约不注册 LLM 工具，且可通过 /comfy run 多轮完成执行。"""
    plugin, server = setup_workflows(tmp_path, monkeypatch)

    async def scenario():
        try:
            await plugin.initialize()
            assert plugin.context.llm_tools == []

            origin = FakeEvent("/comfy run", sender="5", group="2")
            start_out = await run_cmd(plugin, origin)
            assert "1. comfy_cmd_demo" in start_out[0][1]

            pick = FakeEvent("1", sender="5", group="2")
            assert await dispatch_reply(pick) is True
            assert "工作流：comfy_cmd_demo" in sent_texts(pick)[0]
            assert "[1/4] prompt" in sent_texts(pick)[0]
            assert "prefix" not in sent_texts(pick)[0]

            other_user = FakeEvent("你好", sender="6", group="2")
            assert await dispatch_reply(other_user) is False

            t1_short = FakeEvent("a", sender="5", group="2")
            await dispatch_reply(t1_short)
            assert "取值不合法" in sent_texts(t1_short)[0]

            t1 = FakeEvent("一只蓝猫", sender="5", group="2")
            await dispatch_reply(t1)
            assert "[2/4] ratio" in sent_texts(t1)[0]

            t2 = FakeEvent("2", sender="5", group="2")
            await dispatch_reply(t2)
            assert "[3/4] hires" in sent_texts(t2)[0]

            t3 = FakeEvent("是", sender="5", group="2")
            await dispatch_reply(t3)
            assert "[4/4] seed" in sent_texts(t3)[0]
            assert "工作流原值：42" in sent_texts(t3)[0]

            t4 = FakeEvent("跳过", sender="5", group="2")
            await dispatch_reply(t4)
            assert "已提交工作流 comfy_cmd_demo" in sent_texts(t4)[0]

            await asyncio.gather(*list(plugin.jobs.tasks), return_exceptions=True)
            assert len(server.submissions) == 1
            graph = server.submissions[0]
            assert graph["1"]["inputs"]["text"] == "一只蓝猫"
            assert graph["2"]["inputs"]["ratio"] == "16:9"
            assert graph["2"]["inputs"]["hires"] is True
            assert graph["2"]["inputs"]["seed"] == 42
            assert graph["5"]["inputs"]["filename_prefix"].startswith("cmd/20")
            assert any(
                kind == "chain" and isinstance(items[0], Image)
                for kind, items in t4.sent
            )
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_interactive_media_inputs_accept_attachments_and_http_reject_local_paths(
    tmp_path,
    monkeypatch,
):
    """命令模式支持发送附件与 http(s) 链接，并拒绝本地绝对路径与 file:// URI。"""
    img_file = tmp_path / "ref.png"
    img_file.write_bytes(b"png-bytes")
    media_contract = {
        "version": 1,
        "name": "comfy_media",
        "description": "图文测试",
        "model_visible": False,
        "inputs": {
            "ref": {
                "type": "image",
                "description": "参考图",
                "required": True,
                "target": {"node": "3", "field": "image"},
            },
            "doc": {
                "type": "file",
                "description": "说明文件",
                "required": True,
                "target": {"node": "4", "field": "path"},
            },
        },
        "outputs": {
            "image": {"type": "image", "target": {"node": "5", "field": "images"}},
        },
    }
    plugin, server = setup_workflows(
        tmp_path,
        monkeypatch,
        contracts={"media": media_contract},
    )
    server.external_response = (200, b"remote-doc")

    async def scenario():
        try:
            origin = FakeEvent("/comfy run comfy_media", sender="5", group="2")
            out = await run_cmd(plugin, origin)
            assert "[1/2] ref" in out[0][1]

            bad_local = FakeEvent(str(img_file), sender="5", group="2")
            await dispatch_reply(bad_local)
            assert "不支持本地路径" in sent_texts(bad_local)[0]

            bad_uri = FakeEvent(img_file.as_uri(), sender="5", group="2")
            await dispatch_reply(bad_uri)
            assert "不支持本地路径" in sent_texts(bad_uri)[0]

            img_turn = FakeEvent(
                "",
                sender="5",
                group="2",
                components=[Reply(id="r1", chain=[Image(file=str(img_file))])],
            )
            await dispatch_reply(img_turn)
            assert "[2/2] doc" in sent_texts(img_turn)[0]

            url_turn = FakeEvent(
                f"{EXTERNAL_URL_PREFIX}a.txt",
                sender="5",
                group="2",
            )
            await dispatch_reply(url_turn)
            assert "已提交工作流 comfy_media" in sent_texts(url_turn)[0]

            await asyncio.gather(*list(plugin.jobs.tasks), return_exceptions=True)
            assert len(server.uploads) == 2
            assert len(server.submissions) == 1
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_interactive_run_now_and_zero_param_and_cancel(tmp_path, monkeypatch):
    """支持立即执行跳过剩余可选参数、零参数直接提交、取消与防重入。"""
    zero_contract = {
        "version": 1,
        "name": "comfy_zero",
        "description": "无可见参数",
        "model_visible": False,
        "inputs": {},
        "outputs": {
            "image": {"type": "image", "target": {"node": "5", "field": "images"}},
        },
    }
    plugin, server = setup_workflows(
        tmp_path,
        monkeypatch,
        contracts={"demo": CONTRACT, "zero": zero_contract},
    )

    async def scenario():
        try:
            zero_ev = FakeEvent("/comfy run comfy_zero", sender="5", group="2")
            zero_res = await run_cmd(plugin, zero_ev)
            assert "已提交工作流 comfy_zero" in zero_res[0][1]
            await asyncio.gather(*list(plugin.jobs.tasks), return_exceptions=True)
            assert len(server.submissions) == 1

            start = FakeEvent("/comfy run comfy_cmd_demo", sender="5", group="2")
            await run_cmd(plugin, start)

            reenter = FakeEvent("/comfy run comfy_zero", sender="5", group="2")
            assert await dispatch_reply(reenter) is True
            assert "已有正在进行的工作流向导" in sent_texts(reenter)[0]

            t1 = FakeEvent("小橘猫", sender="5", group="2")
            await dispatch_reply(t1)
            t2 = FakeEvent("1", sender="5", group="2")
            await dispatch_reply(t2)
            assert "[3/4] hires" in sent_texts(t2)[0]

            run_now = FakeEvent("立即执行", sender="5", group="2")
            await dispatch_reply(run_now)
            assert "已提交工作流 comfy_cmd_demo" in sent_texts(run_now)[0]
            await asyncio.gather(*list(plugin.jobs.tasks), return_exceptions=True)
            assert len(server.submissions) == 2

            start2 = FakeEvent("/comfy run comfy_cmd_demo", sender="5", group="2")
            await run_cmd(plugin, start2)
            cancel_ev = FakeEvent("取消", sender="5", group="2")
            await dispatch_reply(cancel_ev)
            assert "已取消工作流 comfy_cmd_demo" in sent_texts(cancel_ev)[0]
            assert len(server.submissions) == 2

            idle_cancel = [
                item
                async for item in plugin.cmd_comfy_cancel(
                    FakeEvent("/comfy cancel", sender="5", group="2")
                )
            ]
            assert "没有进行中的工作流向导" in idle_cancel[0][1]
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_interactive_sensitive_cooldown_and_timeout(tmp_path, monkeypatch):
    """命令模式当轮拦截敏感词、提交时检查冷却、并在超时后清理会话。"""
    plugin, server = setup_workflows(
        tmp_path,
        monkeypatch,
        control={"admin_ids": ["9"], "whitelist_ids": ["2"], "cooldown_seconds": 60},
        sensitive_words=["badword"],
    )

    async def scenario():
        try:
            start = FakeEvent("/comfy run comfy_cmd_demo", sender="5", group="2")
            await run_cmd(plugin, start)

            bad = FakeEvent("contains badword", sender="5", group="2")
            await dispatch_reply(bad)
            assert "检测到敏感词" in sent_texts(bad)[0]

            ok1 = FakeEvent("合法文本", sender="5", group="2")
            await dispatch_reply(ok1)
            ok2 = FakeEvent("1", sender="5", group="2")
            await dispatch_reply(ok2)
            done = FakeEvent("立即执行", sender="5", group="2")
            await dispatch_reply(done)
            assert "已提交工作流 comfy_cmd_demo" in sent_texts(done)[0]
            await asyncio.gather(*list(plugin.jobs.tasks), return_exceptions=True)

            start2 = FakeEvent("/comfy run comfy_cmd_demo", sender="5", group="2")
            await run_cmd(plugin, start2)
            await dispatch_reply(FakeEvent("合法文本", sender="5", group="2"))
            await dispatch_reply(FakeEvent("1", sender="5", group="2"))
            cooldown_turn = FakeEvent("立即执行", sender="5", group="2")
            await dispatch_reply(cooldown_turn)
            assert "冷却中" in sent_texts(cooldown_turn)[0]
            assert len(server.submissions) == 1

            plugin.interactive.timeout_seconds = 0.05
            origin = FakeEvent("/comfy run comfy_cmd_demo", sender="7", group="2")
            await run_cmd(plugin, origin)
            await asyncio.sleep(0.12)
            assert "等待输入超时" in sent_texts(origin)[0]
            late = FakeEvent("晚了", sender="7", group="2")
            assert await dispatch_reply(late) is False
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_interactive_run_by_index_and_rejects_extra_args(tmp_path, monkeypatch):
    """支持通过 /comfy run <序号> 直接启动，多参数与越界序号不开启向导。"""
    plugin, server = setup_workflows(tmp_path, monkeypatch)

    async def scenario():
        try:
            extra = FakeEvent("/comfy run 1 一只猫", sender="5", group="2")
            extra_res = await run_cmd(plugin, extra)
            assert "参数请在进入向导后逐轮输入" in extra_res[0][1]
            chat = FakeEvent("普通聊天", sender="5", group="2")
            assert await dispatch_reply(chat) is False

            oob = FakeEvent("/comfy run 9", sender="5", group="2")
            oob_res = await run_cmd(plugin, oob)
            assert "未找到工作流：9" in oob_res[0][1]
            assert "1. comfy_cmd_demo" in oob_res[0][1]
            chat = FakeEvent("普通聊天", sender="5", group="2")
            assert await dispatch_reply(chat) is False

            start = FakeEvent("/comfy run 1", sender="5", group="2")
            out = await run_cmd(plugin, start)
            assert "工作流：comfy_cmd_demo" in out[0][1]
            assert "[1/4] prompt" in out[0][1]

            await dispatch_reply(FakeEvent("一只白猫", sender="5", group="2"))
            await dispatch_reply(FakeEvent("1", sender="5", group="2"))
            done = FakeEvent("立即执行", sender="5", group="2")
            await dispatch_reply(done)
            assert "已提交工作流 comfy_cmd_demo" in sent_texts(done)[0]
            await asyncio.gather(*list(plugin.jobs.tasks), return_exceptions=True)
            assert len(server.submissions) == 1
            assert server.submissions[0]["1"]["inputs"]["text"] == "一只白猫"
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
