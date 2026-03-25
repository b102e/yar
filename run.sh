#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "❌ .venv not found. Run ./install.sh first"
  exit 1
fi

source .venv/bin/activate

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "❌ ANTHROPIC_API_KEY is not set (put it in .env)"
  exit 1
fi

exec python main.py "$@"
