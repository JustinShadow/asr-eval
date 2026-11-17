#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/deploy/compose.test.yml"
UV_EXECUTABLE="${UV_BIN:-$HOME/.local/bin/uv}"
ASR_E2E_DATA_DIR="$(mktemp -d /tmp/asr-eval-e2e.XXXXXX)"
PLAYWRIGHT_RUNTIME_LIB="$HOME/.local/lib/playwright-runtime/usr/lib/x86_64-linux-gnu"

# 真机 E2E 可提供一次性 known_hosts；只包装本次 E2E 子进程的 SSH/SFTP。
if [[ -n "${ASR_E2E_SSH_KNOWN_HOSTS:-}" ]]; then
  if [[ ! -r "$ASR_E2E_SSH_KNOWN_HOSTS" ]]; then
    printf 'ASR_E2E_SSH_KNOWN_HOSTS 不可读\n' >&2
    exit 2
  fi
  export PATH="$REPO_ROOT/tools/e2e-ssh:$PATH"
fi

# WSL 未安装系统浏览器库时，允许使用用户目录下的临时运行库副本。
if [[ -d "$PLAYWRIGHT_RUNTIME_LIB" ]]; then
  export LD_LIBRARY_PATH="$PLAYWRIGHT_RUNTIME_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

cleanup() {
  result=$?
  docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$ASR_E2E_DATA_DIR"
  exit "$result"
}
trap cleanup EXIT INT TERM

# 仅重置 E2E 专用容器；开发数据库使用不同的 Compose 文件和端口。
docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans >/dev/null 2>&1 || true
docker compose -f "$COMPOSE_FILE" up -d --wait db

export ASR_E2E_DATA_DIR
export ASR_E2E_UV="$UV_EXECUTABLE"
export DATABASE_URL="postgresql+psycopg://asr:asr@127.0.0.1:55432/asr_eval_e2e"
export APP_ENV=dev
export AUTH_MODE=local
export JWT_KEY="e2e-only-key-that-is-not-used-outside-this-disposable-test-database"

"$UV_EXECUTABLE" run --directory "$REPO_ROOT/backend" alembic upgrade head
PYTHONPATH="$REPO_ROOT/backend" "$UV_EXECUTABLE" run --directory "$REPO_ROOT/backend" python ../tools/e2e_seed.py

cd "$REPO_ROOT/web"
npm run e2e
