#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN=${PYTHON_BIN:-python3}
VENV_DIR=${VENV_DIR:-.venv}
MODE=${1:-base}

$PYTHON_BIN -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip wheel setuptools

if [[ "$MODE" == "full" ]]; then
  pip install -r requirements-full.txt
else
  pip install -r requirements.txt
fi

echo "✅ Install done. Next:"
echo "1) cp .env.example .env"
echo "2) edit .env and set ANTHROPIC_API_KEY"
echo "3) ./run.sh --web  (or ./run.sh --web --telegram)"
