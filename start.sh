#!/bin/bash
# 启动后端（8000）和前端（5173）

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "启动后端 http://localhost:8000 ..."
cd "$SCRIPT_DIR"
uvicorn app:app --port 8000 &
BACKEND_PID=$!

echo "启动前端 http://localhost:5173 ..."
cd "$SCRIPT_DIR/frontend"
npm run dev &
FRONTEND_PID=$!

echo "后端 PID=$BACKEND_PID  前端 PID=$FRONTEND_PID"
echo "Ctrl+C 同时关闭两个服务"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait
