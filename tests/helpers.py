"""测试替身：假事件与真实插件装配。"""

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
from astrbot.api.star import StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot_plugin_comfy_runner.main import ComfyUIPlugin

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class FakeContext:
    """只实现插件初始化用到的上下文接口。"""

    def __init__(self) -> None:
        """记录被添加的 LLM 工具。"""
        self.llm_tools: list = []

    def add_llm_tools(self, *tools) -> None:
        """记录注册的工具。"""
        self.llm_tools.extend(tools)


class FakeResult:
    """只带消息链的结果对象。"""

    def __init__(self, chain=None) -> None:
        """初始化消息链。

        Args:
            chain: 初始组件。
        """
        self.chain = list(chain or [])


class FakeEvent:
    """只实现命令与钩子用到的那部分事件接口。"""

    def __init__(self, text: str = "", sender: str = "9", group: str = "2") -> None:
        """设置消息文本、发送者与群号。

        Args:
            text: 消息原文。
            sender: 发送者 ID。
            group: 群号，空串表示私聊。
        """
        self.message_str = text
        self.extra: dict = {}
        self.sent: list = []
        self._sender = sender
        self._group = group
        self._result = FakeResult()
        self.unified_msg_origin = "origin"

    def plain_result(self, text: str):
        """构造纯文本结果的替身。"""
        return ("text", text)

    def chain_result(self, components):
        """构造消息链结果的替身。"""
        return ("chain", components)

    async def send(self, result) -> None:
        """把主动发送的结果记下来。"""
        self.sent.append(result)

    def get_sender_id(self) -> str:
        """返回发送者 ID。"""
        return self._sender

    def get_group_id(self) -> str:
        """返回群号。"""
        return self._group

    def set_extra(self, key: str, value) -> None:
        """写入附加数据。"""
        self.extra[key] = value

    def get_extra(self, key: str, default=None):
        """读取附加数据。"""
        return self.extra.get(key, default)

    def get_result(self) -> FakeResult:
        """返回当前结果对象。"""
        return self._result


def make_astrbot_config(tmp_path: Path) -> AstrBotConfig:
    """用插件 schema 构建 AstrBot 配置对象。"""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    return AstrBotConfig(config_path=str(tmp_path / "config.json"), schema=schema)


def make_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **overrides: object,
) -> tuple[ComfyUIPlugin, AstrBotConfig]:
    """在临时数据目录里装配真实插件。"""
    data_dir = tmp_path / "plugin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        StarTools,
        "get_data_dir",
        classmethod(lambda cls, plugin_name=None: data_dir),
    )
    config = make_astrbot_config(tmp_path)
    for section, values in overrides.items():
        if isinstance(values, dict):
            config[section].update(values)
        else:
            config[section] = values
    config.save_config()

    return ComfyUIPlugin(FakeContext(), config), config


def collect(gen) -> list:
    """同步收集异步生成器的输出。

    Args:
        gen: 异步生成器。

    Returns:
        全部产出。
    """
    return asyncio.run(_collect(gen))


async def _collect(gen) -> list:
    return [item async for item in gen]


class FakeResponse:
    """假 HTTP 响应，供真实客户端解析。"""

    def __init__(self, status: int, body: bytes) -> None:
        """记录状态码与响应体。

        Args:
            status: HTTP 状态码。
            body: 响应体字节。
        """
        self.status = status
        self._body = body

    async def text(self) -> str:
        """返回响应体文本。"""
        return self._body.decode("utf-8")

    async def json(self, content_type=None):
        """返回解析后的响应体。"""
        return json.loads(self._body)

    async def read(self) -> bytes:
        """返回响应体字节。"""
        return self._body

    async def __aenter__(self) -> "FakeResponse":
        """进入上下文。"""
        return self

    async def __aexit__(self, *exc_info) -> bool:
        """退出上下文。"""
        return False


class FakeComfyServer:
    """按路由回复的假 ComfyUI，用于断言真实客户端的解析结果。"""

    def __init__(self) -> None:
        """设置默认响应。"""
        self.submissions: list[dict] = []
        self.history_queries: list[str] = []
        self.downloads: list[str] = []
        self.prompt_response = (200, {"prompt_id": "p1", "node_errors": {}})
        self.history_queue: list[dict] = []
        self.history_reply: dict = {}
        self.view_response = (200, b"png-bytes")
        self.object_info: dict[str, dict] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "FakeComfyServer":
        """把 aiohttp 会话替换成这个假服务。"""
        server = self

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            def post(self, url, json=None, timeout=None):
                return server._post(str(url), json or {})

            def get(self, url, timeout=None):
                return server._get(str(url))

        monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
        return self

    def _post(self, url: str, payload: dict) -> FakeResponse:
        if url.endswith("/prompt"):
            self.submissions.append(payload["prompt"])
            return _response(*self.prompt_response)
        raise AssertionError(f"未预期的 POST: {url}")

    def _get(self, url: str) -> FakeResponse:
        if "/history/" in url:
            self.history_queries.append(url.rsplit("/", 1)[-1])
            reply = (
                self.history_queue.pop(0) if self.history_queue else self.history_reply
            )
            return _response(200, reply)
        if "/view?" in url:
            self.downloads.append(url)
            return _response(*self.view_response)
        if "/object_info/" in url:
            return _response(200, self.object_info)
        raise AssertionError(f"未预期的 GET: {url}")


def _response(status: int, body: object) -> FakeResponse:
    if isinstance(body, bytes):
        return FakeResponse(status, body)
    if isinstance(body, str):
        return FakeResponse(status, body.encode("utf-8"))
    return FakeResponse(status, json.dumps(body).encode("utf-8"))
