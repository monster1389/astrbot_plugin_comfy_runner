#!/usr/bin/env bash
# 插件检查：ruff + 无注释 + 无装饰字符 + pytest，全部在 astrbot 容器里跑。
# 三项都跑完再汇总退出，红了能一次看全。用法：scripts/check.sh
set -uo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
container=/AstrBot/data/plugins/astrbot_plugin_comfy_runner

if [ "$(docker inspect -f '{{.State.Running}}' astrbot 2>/dev/null)" != "true" ]; then
  echo "astrbot 容器没在跑，检查无法执行：docker compose -f services/astrbot/docker-compose.yml up -d" >&2
  exit 1
fi

tracked() {
  git -C "$root" ls-files --cached --others --exclude-standard -- "$@"
}

mapfile -t py_files < <(tracked '*.py')
mapfile -t json_files < <(tracked '*.json')

if [ ${#py_files[@]} -eq 0 ] && [ ${#json_files[@]} -eq 0 ]; then
  echo "没有待检查文件"
  exit 0
fi

failed=0

if [ ${#py_files[@]} -gt 0 ]; then
  docker exec -w "$container" astrbot ruff check "${py_files[@]}" || failed=1
  docker exec -w "$container" astrbot python3 scripts/check_no_comments.py "${py_files[@]}" || failed=1
fi

docker exec -w "$container" astrbot python3 scripts/check_no_emoji.py "${py_files[@]}" "${json_files[@]}" || failed=1

if [ -d "$root/tests" ]; then
  docker exec -w "$container" astrbot python -m pytest tests -q || failed=1
fi

if [ "$failed" -ne 0 ]; then
  echo "检查未通过" >&2
  exit 1
fi

echo "全部检查通过"
