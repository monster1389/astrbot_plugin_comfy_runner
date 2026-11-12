#!/usr/bin/env python3
"""拒绝装饰性字符，只放行 ✅ ❌ ⚠️ 🚫 四个状态符号。"""

import re
import sys
from pathlib import Path

ALLOWED_SEQUENCES = ("\u2705", "\u274c", "\u26a0\ufe0f", "\u26a0", "\U0001f6ab")
FORBIDDEN = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\u2190-\u21ff"
    "\u2300-\u23ff"
    "\u2500-\u257f"
    "\u25a0-\u27bf"
    "\u2b00-\u2bff"
    "\ufe0f"
    "]"
)
TARGET_SUFFIXES = {".py", ".json"}


def scan_text(text: str, label: str) -> list[str]:
    """在文本里找出违规字符。

    Args:
        text: 待检查的文本。
        label: 出现在报告里的文件名。

    Returns:
        违规描述列表。
    """
    cleaned = text
    for sequence in ALLOWED_SEQUENCES:
        cleaned = cleaned.replace(sequence, " ")

    violations = []
    for number, line in enumerate(cleaned.splitlines(), start=1):
        for match in FORBIDDEN.finditer(line):
            violations.append(f"{label}:{number}: 禁止装饰字符 {match.group(0)!r}")
    return violations


def main(paths: list[str]) -> int:
    """检查给定文件并打印违规项。

    Args:
        paths: 待检查的文件路径。

    Returns:
        进程退出码，有违规时为 1。
    """
    violations = []
    for raw in paths:
        path = Path(raw)
        if path.suffix not in TARGET_SUFFIXES:
            continue
        violations.extend(scan_text(path.read_text(encoding="utf-8"), str(path)))

    for line in violations:
        print(line)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
