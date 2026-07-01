#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
BACKEND_START_PORT="${BACKEND_PORT:-8000}"
FRONTEND_START_PORT="${FRONTEND_PORT:-5173}"

find_free_port() {
  "$PYTHON" - "$1" <<'PY'
import socket
import sys

port = int(sys.argv[1])
while port < 65535:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            port += 1
            continue
        print(port)
        raise SystemExit(0)

raise SystemExit("没有可用端口")
PY
}

stop_services() {
  kill "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
}

trap stop_services INT TERM EXIT

cd "$SCRIPT_DIR"

if [ ! -x "$PYTHON" ]; then
  echo "未找到 .venv，正在创建 Python 虚拟环境..."
  python3 -m venv "$SCRIPT_DIR/.venv"
fi

if ! "$PYTHON" -m uvicorn --version >/dev/null 2>&1; then
  echo "正在安装后端依赖..."
  "$PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt"
fi

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "正在安装前端依赖..."
  (cd "$FRONTEND_DIR" && npm install)
fi

BACKEND_PORT="$(find_free_port "$BACKEND_START_PORT")"
FRONTEND_PORT="$(find_free_port "$FRONTEND_START_PORT")"
BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
FRONTEND_URL="http://127.0.0.1:$FRONTEND_PORT"

echo "启动后端 $BACKEND_URL ..."
"$PYTHON" -m uvicorn app:app --host 127.0.0.1 --port "$BACKEND_PORT" &
BACKEND_PID=$!

sleep 1
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
  echo "后端启动失败"
  exit 1
fi

echo "启动前端 $FRONTEND_URL ..."
(
  cd "$FRONTEND_DIR"
  VITE_PROXY_TARGET="$BACKEND_URL" ./node_modules/.bin/vite --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort
) &
FRONTEND_PID=$!

sleep 1
if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
  echo "前端启动失败"
  exit 1
fi

echo
echo "启动完成"
echo "后端：$BACKEND_URL"
echo "前端：$FRONTEND_URL"
echo "Ctrl+C 同时关闭两个服务"

wait
