#!/usr/bin/env bash
# Runtime wrapper. Set WEBHOOK_URL or WEBHOOK_PUBLIC_IP for webhook mode.
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
