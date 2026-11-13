"""测试替身：假事件、假客户端与最小配置。"""

import asyncio
from pathlib import Path

from comfy_runner.access import AccessController
from comfy_runner.commands import CommandContext
from comfy_runner.config import Config
from comfy_runner.draw import Drawer


class FakeAPI:
    """固定返回一张图片的出图客户端。"""

    def __init__(self, data: bytes = b"png-bytes") -> None:
        """记录出图调用并准备返回内容。

        Args:
            data: 出图返回的字节。
        """
        self.data = data
        self.calls: list[str] = []

    async def generate(self, prompt: str, lora_selections=None):
        """记录提示词并返回固定字节。"""
        self.calls.append(prompt)
        return self.data, None


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
        self._comfy_segments = None
        self._comfy_extracted_prompt = None
        self._comfy_extracted_loras = []
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

    def get_self_id(self) -> str:
        """返回机器人自身账号。"""
        return "999"

    def set_extra(self, key: str, value) -> None:
        """写入附加数据。"""
        self.extra[key] = value

    def get_extra(self, key: str, default=None):
        """读取附加数据。"""
        return self.extra.get(key, default)

    def get_result(self) -> FakeResult:
        """返回当前结果对象。"""
        return self._result

    def set_result(self, result: FakeResult) -> None:
        """替换当前结果对象。"""
        self._result = result


RAW = {
    "server_address": "127.0.0.1:8188",
    "workflow_settings": {
        "json_file": "wf.json",
        "input_node_id": "1",
        "neg_node_id": "2",
        "output_node_id": "3",
    },
    "llm_settings": {
        "multi_image_mode": False,
        "discard_prompt_from_history": False,
        "force_draw_when_no_prompt": False,
        "target_image_count": 1,
        "system_prompt": "system",
        "lora_control": {
            "enabled": False,
            "inject_catalog_into_system_prompt": True,
            "inject_selected_prompt_hints": True,
            "keep_workflow_defaults_when_selected": True,
            "max_lora_count": 4,
            "scan_directories": "",
            "max_trigger_options_per_lora": 3,
            "trigger_option_preview_length": 80,
        },
    },
    "control": {
        "cooldown_seconds": 0,
        "admin_ids": ["9"],
        "whitelist_group_ids": ["2"],
        "default_group_policy": "lite",
        "default_private_policy": "lite",
        "lockdown": False,
        "lockdown_command_enabled": True,
        "admin_bypass": {
            "whitelist": True,
            "cooldown": True,
            "sensitive_words": True,
        },
    },
}


def make_config(tmp_path: Path, **overrides) -> Config:
    """用最小配置构建 Config。

    Args:
        tmp_path: 数据目录。
        overrides: 按顶层键覆盖的配置片段。

    Returns:
        配置对象。
    """
    raw = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in RAW.items()
    }
    raw["llm_settings"] = dict(RAW["llm_settings"])
    raw["control"] = dict(RAW["control"])
    for section, values in overrides.items():
        if isinstance(values, dict):
            raw[section] = {**raw[section], **values}
        else:
            raw[section] = values
    return Config.from_mapping(raw, tmp_path)


def make_ctx(tmp_path: Path, api: FakeAPI | None = None, **overrides) -> CommandContext:
    """构建命令上下文。

    Args:
        tmp_path: 数据目录，也是图片落盘目录。
        api: 出图客户端替身。
        overrides: 配置覆盖。

    Returns:
        命令上下文。
    """
    cfg = make_config(tmp_path, **overrides)
    access = AccessController(cfg.control, {})
    api = api or FakeAPI()
    drawer = Drawer(api, access, tmp_path)
    return CommandContext(cfg, access, api, drawer, None)


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
