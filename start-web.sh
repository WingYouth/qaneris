#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
frontend_dir="$repo_root/web/frontend"
backend_pid=""
frontend_pid=""
url="http://127.0.0.1:5173/"

backend_is_healthy() {
  curl --silent --fail --max-time 2 http://127.0.0.1:8000/health >/dev/null
}

backend_has_current_routes() {
  curl --silent --fail --max-time 2 http://127.0.0.1:8000/openapi.json |
    "$repo_root/.venv/bin/python" -c 'import json, sys; schema = json.load(sys.stdin); assert "delete" in schema["paths"]["/api/conversations/{conversation_id}"]' 2>/dev/null
}

restart_stale_backend() {
  local listener_pid listener_cwd
  if ! command -v lsof >/dev/null 2>&1; then
    echo "现有后端缺少当前接口，且找不到 lsof，无法安全确认进程归属。请停止旧后端后重试。" >&2
    return 1
  fi
  listener_pid="$(lsof -nP -t -iTCP:8000 -sTCP:LISTEN | head -n 1)"
  listener_cwd="$(lsof -a -p "$listener_pid" -d cwd -Fn | sed -n 's/^n//p' | head -n 1)"
  if [[ -z "$listener_pid" || "$listener_cwd" != "$repo_root" ]]; then
    echo "现有后端缺少当前接口，但 8000 端口的进程不属于当前仓库。请先检查该进程。" >&2
    return 1
  fi
  echo "检测到当前仓库的旧版后端（PID ${listener_pid}），正在重启…"
  kill -TERM "$listener_pid"
  for ((attempt = 0; attempt < 50; attempt++)); do
    if ! kill -0 "$listener_pid" 2>/dev/null; then return 0; fi
    sleep 0.2
  done
  echo "旧后端未退出，请检查 PID ${listener_pid}。" >&2
  return 1
}

cleanup() {
  if [[ -n "$frontend_pid" ]]; then kill "$frontend_pid" 2>/dev/null || true; fi
  if [[ -n "$backend_pid" ]]; then kill "$backend_pid" 2>/dev/null || true; fi
  if [[ -n "$frontend_pid" ]]; then wait "$frontend_pid" 2>/dev/null || true; fi
  if [[ -n "$backend_pid" ]]; then wait "$backend_pid" 2>/dev/null || true; fi
}
trap cleanup EXIT

if [[ ! -x "$repo_root/.venv/bin/python" ]]; then
  echo "找不到 .venv/bin/python。请先在仓库根目录安装 Python 依赖。" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "找不到 npm。请先安装 Node.js。" >&2
  exit 1
fi

cd "$repo_root"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ ! -d "$frontend_dir/node_modules" ]]; then
  echo "正在安装前端依赖…"
  (cd "$frontend_dir" && npm ci --prefer-offline --no-audit)
fi

if backend_is_healthy && ! backend_has_current_routes; then
  restart_stale_backend
fi

if backend_is_healthy; then
  echo "后端已在 127.0.0.1:8000 运行，当前接口已加载。"
else
  "$repo_root/.venv/bin/python" -m uvicorn qaneris.interfaces.api.app:app --host 127.0.0.1 --port 8000 &
  backend_pid=$!
  for ((attempt = 0; attempt < 50; attempt++)); do
    if backend_is_healthy; then break; fi
    if ! kill -0 "$backend_pid" 2>/dev/null; then echo "后端启动失败。" >&2; exit 1; fi
    sleep 0.2
  done
  if ! backend_is_healthy; then
    echo "后端健康检查超时。" >&2
    exit 1
  fi
fi
if ! backend_has_current_routes; then
  echo "后端缺少当前对话接口，启动失败。" >&2
  exit 1
fi

if curl --silent --fail --max-time 2 "$url" >/dev/null; then
  echo "前端已在 $url 运行。"
else
  (cd "$frontend_dir" && exec ./node_modules/.bin/vite --host 127.0.0.1 --port 5173 --strictPort) &
  frontend_pid=$!
  for ((attempt = 0; attempt < 50; attempt++)); do
    if curl --silent --fail --max-time 2 "$url" >/dev/null; then break; fi
    if ! kill -0 "$frontend_pid" 2>/dev/null; then echo "前端启动失败。" >&2; exit 1; fi
    sleep 0.2
  done
  if ! curl --silent --fail --max-time 2 "$url" >/dev/null; then
    echo "前端健康检查超时。" >&2
    exit 1
  fi
fi

echo "Qaneris 已启动：$url"
echo "按 Ctrl+C 停止由本脚本启动的服务。"
if [[ "${1:-}" != "--no-open" ]] && command -v open >/dev/null 2>&1; then
  open "$url" || true
fi

while [[ -n "$backend_pid" || -n "$frontend_pid" ]]; do
  if [[ -n "$backend_pid" ]] && ! kill -0 "$backend_pid" 2>/dev/null; then
    echo "后端进程已退出。" >&2
    exit 1
  fi
  if [[ -n "$frontend_pid" ]] && ! kill -0 "$frontend_pid" 2>/dev/null; then
    echo "前端进程已退出。" >&2
    exit 1
  fi
  sleep 1
done
