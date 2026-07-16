#!/usr/bin/env bash
# Deploy ShopGPT auto-sign to production server and install daily cron.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DEPLOY_HOST="${DEPLOY_HOST:-101.245.78.20}"
DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/101.245.78.20_id_ed25519}"
REMOTE_DIR="${DEPLOY_DIR:-/opt/shopgpt_auto_sign}"
CRON_SCHEDULE="${CRON_SCHEDULE:-5 0 * * *}"
UPLOAD_ACCOUNTS="${UPLOAD_ACCOUNTS:-1}"
DRY_RUN=0
SKIP_CRON=0
RUN_ONCE=0

usage() {
  cat <<'EOF'
Usage: ./scripts/deploy-production.sh [options]

Options:
  --dry-run          Preview rsync only; do not change the server
  --skip-accounts    Do not upload local accounts.json
  --skip-cron        Do not install/update remote crontab
  --run-once         After deploy, run one sign job on the server
  -h, --help         Show help

Environment:
  DEPLOY_HOST, DEPLOY_USER, DEPLOY_SSH_KEY, DEPLOY_DIR, CRON_SCHEDULE
EOF
}

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while (($# > 0)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --skip-accounts) UPLOAD_ACCOUNTS=0 ;;
    --skip-cron) SKIP_CRON=1 ;;
    --run-once) RUN_ONCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

command -v ssh >/dev/null || die "ssh is required"
command -v rsync >/dev/null || die "rsync is required"
[[ -f "$DEPLOY_SSH_KEY" ]] || die "SSH key not found: $DEPLOY_SSH_KEY"
[[ -f "$ROOT_DIR/sign.py" && -f "$ROOT_DIR/requirements.txt" ]] || \
  die "run this script from the shopgpt_auto_sign repository"

REMOTE="${DEPLOY_USER}@${DEPLOY_HOST}"
SSH_OPTS=(
  -i "$DEPLOY_SSH_KEY"
  -o BatchMode=yes
  -o ConnectTimeout=15
  -o IdentitiesOnly=yes
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=4
  -o StrictHostKeyChecking=accept-new
)

remote_run() {
  ssh "${SSH_OPTS[@]}" "$REMOTE" "$@"
}

RSYNC_EXCLUDES=(
  --exclude '/.git/'
  --exclude '/.venv/'
  --exclude '/venv/'
  --exclude '/__pycache__/'
  --exclude '/**/__pycache__/'
  --exclude '/.codegraph/'
  --exclude '/.cursor/'
  --exclude '/.omc/'
  --exclude '/logs/'
  --exclude '/cookies/'
  --exclude '/captcha.png'
  --exclude '/shopgpt_cookies.json'
  --exclude '/cookies.txt'
  --exclude '/accounts.json'
  --exclude '**/*.pyc'
  --exclude '**/.DS_Store'
)

log "Deploy target: ${REMOTE}:${REMOTE_DIR}"
log "Local root: ${ROOT_DIR}"

if ((DRY_RUN == 1)); then
  rsync -azn --delete "${RSYNC_EXCLUDES[@]}" \
    -e "ssh ${SSH_OPTS[*]}" \
    "$ROOT_DIR/" "$REMOTE:$REMOTE_DIR/"
  log "Dry run complete; production was not changed."
  exit 0
fi

log "Ensure remote directory exists"
remote_run "mkdir -p '$REMOTE_DIR' '$REMOTE_DIR/logs' '$REMOTE_DIR/cookies'"

log "Sync project files"
rsync -az --delete "${RSYNC_EXCLUDES[@]}" \
  -e "ssh ${SSH_OPTS[*]}" \
  "$ROOT_DIR/" "$REMOTE:$REMOTE_DIR/"

if ((UPLOAD_ACCOUNTS == 1)); then
  if [[ -f "$ROOT_DIR/accounts.json" ]]; then
    log "Upload accounts.json (mode 600)"
    rsync -az \
      -e "ssh ${SSH_OPTS[*]}" \
      "$ROOT_DIR/accounts.json" "$REMOTE:$REMOTE_DIR/accounts.json"
    remote_run "chmod 600 '$REMOTE_DIR/accounts.json'"
  else
    warn "local accounts.json missing; skip upload"
  fi
else
  log "Skip accounts.json upload"
fi

log "Create venv and install dependencies"
remote_run bash -s -- "$REMOTE_DIR" <<'REMOTE_SETUP'
set -Eeuo pipefail
app_dir="$1"
cd "$app_dir"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
chmod +x run_sign.sh sign.py
# smoke import
python - <<'PY'
import shopgpt_login
import sign
print("import_ok", shopgpt_login.BASE_URL)
PY
REMOTE_SETUP

if ((SKIP_CRON == 0)); then
  log "Install cron: ${CRON_SCHEDULE}"
  # 整段远程命令作为单字符串，避免 "5 0 * * *" 在 ssh 侧被二次拆词
  remote_run "APP_DIR=$(printf %q "$REMOTE_DIR") CRON_SCHEDULE=$(printf %q "$CRON_SCHEDULE") bash -s" <<'REMOTE_CRON'
set -Eeuo pipefail
app_dir="${APP_DIR:?}"
schedule="${CRON_SCHEDULE:?}"
cron_line="${schedule} /bin/bash ${app_dir}/run_sign.sh"
tmp="$(mktemp)"
crontab -l 2>/dev/null | grep -v 'shopgpt_auto_sign/run_sign.sh' | grep -v "${app_dir}/run_sign.sh" >"$tmp" || true
if [[ -s "$tmp" ]] && [[ "$(tail -c1 "$tmp" | wc -l)" -eq 0 ]]; then
  printf '\n' >>"$tmp"
fi
printf '%s\n' "$cron_line" >>"$tmp"
crontab "$tmp"
rm -f "$tmp"
echo "current crontab:"
crontab -l
REMOTE_CRON
else
  log "Skip cron install"
fi

if ((RUN_ONCE == 1)); then
  log "Run one sign job on server"
  remote_run "cd '$REMOTE_DIR' && /bin/bash ./run_sign.sh; echo exit:\$?; tail -n 80 logs/sign-\$(date +%Y%m%d).log || true"
fi

log "Remote tree"
remote_run "ls -la '$REMOTE_DIR' && test -x '$REMOTE_DIR/.venv/bin/python' && '$REMOTE_DIR/.venv/bin/python' -c 'import ddddocr; print(\"ddddocr_ok\")'"

log "Deployment completed successfully."
printf 'Remote app: %s:%s\n' "$REMOTE" "$REMOTE_DIR"
printf 'Cron: %s /bin/bash %s/run_sign.sh\n' "$CRON_SCHEDULE" "$REMOTE_DIR"
printf 'Logs: %s/logs/sign-YYYYMMDD.log\n' "$REMOTE_DIR"
