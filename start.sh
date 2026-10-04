#!/usr/bin/env bash
# macOS / Linux 本地演示启动脚本：首次执行会创建虚拟环境并安装依赖。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8000}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$ROOT_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "未找到 $PYTHON_BIN，请先安装 Python 3.10 或更高版本。" >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "需要 Python 3.10 或更高版本；可通过 PYTHON_BIN 指定解释器，例如 PYTHON_BIN=python3.12 ./start.sh。" >&2
  exit 1
fi

if [ ! -x "venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv venv
fi

venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -r requirements.txt
echo "校园科创导航智能体已启动：http://127.0.0.1:${PORT}/"
exec venv/bin/python -m uvicorn api:app --app-dir src --host 127.0.0.1 --port "$PORT"
