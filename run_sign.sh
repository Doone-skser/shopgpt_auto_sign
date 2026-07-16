#!/bin/zsh
# cron / 手动均可调用的多账号签到入口
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

mkdir -p "$ROOT/logs" "$ROOT/cookies"

PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"

LOG="$ROOT/logs/sign-$(date +%Y%m%d).log"
{
  echo "======== $(date '+%Y-%m-%d %H:%M:%S') start ========"
  "$PYTHON" "$ROOT/sign.py" --accounts "$ROOT/accounts.json" "$@"
  code=$?
  echo "======== $(date '+%Y-%m-%d %H:%M:%S') end exit=$code ========"
  exit $code
} >>"$LOG" 2>&1
