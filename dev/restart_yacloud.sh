#!/usr/bin/env bash
# Restart the yacloud Telegram bot with optional remote poker state/log cleanup.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

YC_SSH="${YC_SSH:-yc-user@yacloud}"
REMOTE_DIR="${YC_REMOTE_DIR:-~/poker_bot}"
RESET_STATE=false
RESET_LOGS=false
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage:
  dev/restart_yacloud.sh [--keep] [--reset-state] [--reset-logs] [--reset-all] [--dry-run]

Modes:
  --keep          Restart only. Preserve poker stacks, room state, bot.log, and nohup.out. Default.
  --reset-state   Remove remote poker room state JSON and its .bak file before restart.
  --reset-logs    Truncate remote BOT_LOG_PATH and nohup.out before restart.
  --reset-all     Equivalent to --reset-state --reset-logs.
  --dry-run       Print the SSH cleanup and deploy commands without running them.

Environment:
  YC_SSH          SSH target. Default: yc-user@yacloud
  YC_REMOTE_DIR   Remote app directory. Default: ~/poker_bot
  YC_SSH_OPTS     Extra ssh options passed through to ssh and deploy_yacloud.sh.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep)
      ;;
    --reset-state)
      RESET_STATE=true
      ;;
    --reset-logs)
      RESET_LOGS=true
      ;;
    --reset-all)
      RESET_STATE=true
      RESET_LOGS=true
      ;;
    --dry-run)
      DRY_RUN=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

remote_cleanup_script() {
  cat <<'REMOTE_EOF'
set -euo pipefail
REMOTE_DIR="$1"
RESET_STATE="$2"
RESET_LOGS="$3"

expand_remote_dir() {
  local p="$1"
  case "$p" in
    "~") printf '%s' "$HOME" ;;
    "~/"*) printf '%s' "$HOME/${p#~/}" ;;
    *) printf '%s' "$p" ;;
  esac
}

DIR="$(expand_remote_dir "$REMOTE_DIR")"
cd "$DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ "$RESET_LOGS" == "true" ]]; then
  : > "${BOT_LOG_PATH:-bot.log}"
  : > nohup.out
fi

if [[ "$RESET_STATE" == "true" ]]; then
  rm -f "${POKER_STATE_PATH:-data/poker_room_state.json}"
  rm -f "${POKER_STATE_PATH:-data/poker_room_state.json}.bak"
fi
REMOTE_EOF
}

run_remote_cleanup() {
  if [[ "$RESET_STATE" != "true" && "$RESET_LOGS" != "true" ]]; then
    return
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    printf 'ssh %s %s bash -s %q %q %q <<REMOTE_EOF\n' "${YC_SSH_OPTS-}" "$YC_SSH" "$REMOTE_DIR" "$RESET_STATE" "$RESET_LOGS"
    remote_cleanup_script
    printf 'REMOTE_EOF\n'
    return
  fi

  echo "→ remote cleanup (state=$RESET_STATE logs=$RESET_LOGS)"
  ssh ${YC_SSH_OPTS-} "$YC_SSH" bash -s "$REMOTE_DIR" "$RESET_STATE" "$RESET_LOGS" <<<"$(remote_cleanup_script)"
}

run_restart() {
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "dev/deploy_yacloud.sh --run"
    return
  fi

  dev/deploy_yacloud.sh --run
}

run_remote_cleanup
run_restart
