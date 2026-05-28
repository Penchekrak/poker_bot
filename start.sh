#!/usr/bin/env bash
# Long polling. Usage on server: export BOT_TOKEN=... ; nohup ./start.sh >>bot.log 2>&1 &
set -euo pipefail
export PYTHONUNBUFFERED=1
cd "$(dirname "$0")"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
if [[ -z "${BOT_TOKEN:-}" ]]; then
  echo "Set BOT_TOKEN or create .env with BOT_TOKEN=…" >&2
  exit 1
fi
exec .venv/bin/python main.py
