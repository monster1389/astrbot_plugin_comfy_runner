#!/usr/bin/env python3
"""拒绝 .py 文件里的 # 注释，只放行 # noqa 与 # type: ignore。"""

import sys
import tokenize
from pathlib import Path

ALLOWED_PREFIXES = ("# noqa", "# type: ignore")


def find_comments(path: Path) -> list[tuple[int, str]]:
    """返回文件里所有不允许的注释。

    Args:
        path: 待检查的 Python 文件。

    Returns:
        (行号, 注释原文) 列表。
    """
    found = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type != tokenize.COMMENT:
                continue
            text = token.string.strip()
            if text.startswith(ALLOWED_PREFIXES):
                continue
            if token.start[0] == 1 and text.startswith("#!"):
                continue
            found.append((token.start[0], text))
    return found


def main(paths: list[str]) -> int:
    """检查给定文件并打印违规项。

    Args:
        paths: 待检查的文件路径。

    Returns:
        进程退出码，有违规时为 1。
    """
    violations = 0
    for raw in paths:
        path = Path(raw)
        for line, text in find_comments(path):
            print(f"{path}:{line}: 禁止 # 注释：{text}")
            violations += 1
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
