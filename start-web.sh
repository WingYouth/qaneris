#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
frontend_dir="$repo_root/web/frontend"
backend_pid=""
frontend_pid=""
url="http://127.0.0.1:5173/"

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

if curl --silent --fail --max-time 2 http://127.0.0.1:8000/health >/dev/null; then
  echo "后端已在 127.0.0.1:8000 运行。"
else
  "$repo_root/.venv/bin/python" -m uvicorn smartdata.interfaces.api.app:app --host 127.0.0.1 --port 8000 &
  backend_pid=$!
  for ((attempt = 0; attempt < 50; attempt++)); do
    if curl --silent --fail --max-time 2 http://127.0.0.1:8000/health >/dev/null; then break; fi
    if ! kill -0 "$backend_pid" 2>/dev/null; then echo "后端启动失败。" >&2; exit 1; fi
    sleep 0.2
  done
  if ! curl --silent --fail --max-time 2 http://127.0.0.1:8000/health >/dev/null; then
    echo "后端健康检查超时。" >&2
    exit 1
  fi
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

echo "SmartData 已启动：$url"
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
