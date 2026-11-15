"""访问控制：白名单、全局锁定、冷却与敏感词。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .config import POLICIES, ControlSettings

POLICY_CATEGORIES = {
    "none": set(),
    "lite": {"legacy_lite"},
    "full": {
        "legacy_lite",
        "minors",
        "sexual_violence",
        "bestiality_incest_necrophilia",
        "violence_gore",
        "scat_urine_vomit",
        "self_harm",
        "sexual",
        "nudity",
        "fetish",
    },
}


def load_lexicon(path: Path) -> dict[str, list[str]]:
    """读取敏感词库。

    Args:
        path: 词库文件路径。

    Returns:
        类别到词条的映射。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 结构不是「类别到字符串列表」的映射。
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"敏感词库 {path} 必须是对象")

    for category, words in data.items():
        if not isinstance(words, list) or not all(
            isinstance(word, str) for word in words
        ):
            raise ValueError(f"敏感词库 {path} 的 {category} 必须是字符串列表")

    return data


def platform_name(event: AstrMessageEvent) -> str:
    """取事件所属平台类型名。

    Args:
        event: 消息事件。

    Returns:
        平台类型名，取不到时为空串。
    """
    getter = getattr(event, "get_platform_name", None)
    value = getter() if callable(getter) else ""
    return str(value).strip() if value else ""


def scoped_id(event: AstrMessageEvent, raw_id: str) -> str:
    """为 ID 加上当前平台前缀。

    Args:
        event: 消息事件。
        raw_id: 平台内的原始用户或群 ID。

    Returns:
        形如 ``平台:ID`` 的标识，平台为空时返回原值。
    """
    plat = platform_name(event)
    return f"{plat}:{raw_id}" if plat else raw_id


def is_group_message(event: AstrMessageEvent) -> bool:
    """判断事件是否来自群聊。

    Args:
        event: 消息事件。

    Returns:
        群号非空时为 True。
    """
    return bool(event.get_group_id())


def group_id(event: AstrMessageEvent) -> str | None:
    """取事件所属群号。

    Args:
        event: 消息事件。

    Returns:
        群号，非群聊时为 None。
    """
    value = event.get_group_id()
    return str(value) if value else None


def _group_candidates(gid: str) -> tuple[str, ...]:
    if "#" in gid:
        return (gid, gid.split("#", 1)[0])
    return (gid,)


def _matches_whitelist(
    whitelist: frozenset[str],
    plat: str,
    scope: str,
    candidates: tuple[str, ...],
) -> bool:
    for candidate in candidates:
        keys = [candidate, f"{scope}:{candidate}"]
        if plat:
            keys.extend((f"{plat}:{candidate}", f"{plat}:{scope}:{candidate}"))
        if any(key in whitelist for key in keys):
            return True
    return False


def _is_ascii_term(term: str) -> bool:
    return all(ord(char) < 128 for char in term)


class AccessController:
    """一次运行内的访问状态与判定。"""

    def __init__(
        self,
        settings: ControlSettings,
        lexicon: dict[str, list[str]],
    ) -> None:
        """按配置装配并编译敏感词匹配模式。

        Args:
            settings: 访问控制配置。
            lexicon: 敏感词库。
        """
        self.settings = settings
        self.admin_ids = settings.admin_ids
        self.whitelist_ids = settings.whitelist_ids
        self.lockdown = settings.lockdown
        self.group_policies: dict[str, str] = {}
        self.cooldowns: dict[str, float] = {}
        self.lexicon = lexicon
        self.patterns = self._build_patterns()

    def is_admin(self, event: AstrMessageEvent) -> bool:
        """判断事件发送者是否为管理员。

        Args:
            event: 消息事件。

        Returns:
            命中 ``admin_ids``（精确 ``平台:ID`` 或纯 ``ID``）时为 True。
        """
        user_id = str(event.get_sender_id())
        plat = platform_name(event)
        if plat and f"{plat}:{user_id}" in self.admin_ids:
            return True
        return user_id in self.admin_ids

    def is_whitelisted(self, event: AstrMessageEvent) -> bool:
        """判断当前会话（群或私聊用户）是否在白名单内。

        Args:
            event: 消息事件。

        Returns:
            命中白名单时为 True。
        """
        plat = platform_name(event)
        if is_group_message(event):
            gid = group_id(event)
            if not gid:
                return False
            return _matches_whitelist(
                self.whitelist_ids,
                plat,
                "group",
                _group_candidates(gid),
            )
        user_id = str(event.get_sender_id())
        return _matches_whitelist(self.whitelist_ids, plat, "user", (user_id,))

    def check_access(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """检查全局锁定与白名单。

        Args:
            event: 消息事件。

        Returns:
            是否放行，以及拒绝原因。
        """
        is_admin = self.is_admin(event)

        if self.lockdown and not is_admin:
            return False, "🚫 工作流工具已全局锁定，仅管理员可用"

        bypass = is_admin and self.settings.admin_bypass.whitelist
        if is_group_message(event):
            gid = group_id(event)
            if not gid:
                return False, "⚠️ 无法获取群号"
            if not self.is_whitelisted(event) and not bypass:
                return False, f"🚫 本群({scoped_id(event, gid)})不在白名单中"
        elif not self.is_whitelisted(event) and not bypass:
            user_id = str(event.get_sender_id())
            return False, f"🚫 当前用户({scoped_id(event, user_id)})不在白名单中"

        return True, ""

    def check_cooldown(self, event: AstrMessageEvent) -> tuple[bool, int]:
        """检查并更新用户冷却。

        Args:
            event: 消息事件。

        Returns:
            是否放行，以及剩余秒数。
        """
        if self.is_admin(event) and self.settings.admin_bypass.cooldown:
            return True, 0

        key = scoped_id(event, str(event.get_sender_id()))
        now = time.time()
        last_time = self.cooldowns.get(key, 0)
        elapsed = now - last_time

        if elapsed < self.settings.cooldown_seconds:
            return False, int(self.settings.cooldown_seconds - elapsed)

        self.cooldowns[key] = now
        return True, 0

    def check_sensitive(
        self,
        text: str,
        event: AstrMessageEvent,
    ) -> tuple[bool, list[str]]:
        """检查文本里的敏感词。

        Args:
            text: 待检查文本。
            event: 消息事件。

        Returns:
            是否放行，以及触发的词。
        """
        sensitive = self.find_sensitive_words(text, event)
        if not sensitive:
            return True, []
        if self.is_admin(event) and self.settings.admin_bypass.sensitive_words:
            actor = scoped_id(event, str(event.get_sender_id()))
            logger.info(f"[ComfyUI] 管理员 {actor} 使用敏感词 {sensitive}，已放行")
            return True, []
        return False, sensitive

    def policy_for(self, event: AstrMessageEvent) -> str:
        """取当前会话适用的违禁级别。

        Args:
            event: 消息事件。

        Returns:
            级别名。
        """
        if is_group_message(event):
            gid = group_id(event)
            if not gid:
                return self.settings.default_group_policy
            for candidate in _group_candidates(gid):
                key = scoped_id(event, candidate)
                if key in self.group_policies:
                    return self.group_policies[key]
            return self.settings.default_group_policy
        return self.settings.default_private_policy

    def set_policy(self, gid: str, level: str) -> None:
        """设置某个群的违禁级别。

        Args:
            gid: 群号。
            level: 级别名。

        Raises:
            ValueError: 级别不在允许集合内。
        """
        if level not in POLICIES:
            raise ValueError(f"未知的违禁级别: {level}")
        self.group_policies[gid] = level

    def find_sensitive_words(
        self,
        text: str,
        event: AstrMessageEvent | None = None,
    ) -> list[str]:
        """在文本里查找敏感词。

        Args:
            text: 待检查文本。
            event: 消息事件，缺省时按最高级别检查。

        Returns:
            去重后的命中词。
        """
        if not text:
            return []

        policy = self.policy_for(event) if event is not None else "full"
        if policy == "none":
            return []

        pattern = self.patterns.get(policy.lower())
        if not pattern:
            return []

        seen: set[str] = set()
        result: list[str] = []
        for match in pattern.finditer(text):
            word = match.group(0)
            key = word.lower()
            if key not in seen:
                seen.add(key)
                result.append(word)
        return result

    def _build_patterns(self) -> dict[str, re.Pattern[str] | None]:
        patterns: dict[str, re.Pattern[str] | None] = {}
        for policy, categories in POLICY_CATEGORIES.items():
            word_terms: list[str] = []
            phrase_terms: list[str] = []
            for category in categories:
                for term in self.lexicon.get(category, []):
                    if not term or not _is_ascii_term(term):
                        continue
                    if " " in term:
                        phrase_terms.append(re.escape(term))
                    else:
                        word_terms.append(re.escape(term))

            word_terms = list(dict.fromkeys(word_terms))
            phrase_terms = list(dict.fromkeys(phrase_terms))

            parts: list[str] = []
            if word_terms:
                parts.append(
                    r"(?<![A-Za-z0-9_])(?:"
                    + "|".join(word_terms)
                    + r")(?![A-Za-z0-9_])"
                )
            if phrase_terms:
                parts.append("|".join(phrase_terms))

            patterns[policy] = (
                re.compile("|".join(parts), re.IGNORECASE) if parts else None
            )
        return patterns
