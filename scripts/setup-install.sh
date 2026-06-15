#!/usr/bin/env bash
# Install Node + Python deps for solana-arb-bot
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== setup:install ==="
npm install

PY=python3
if [[ -f "$ROOT/venv/bin/python" ]]; then
  PY="$ROOT/venv/bin/python"
elif [[ ! -d "$ROOT/venv" ]]; then
  python3 -m venv "$ROOT/venv"
  PY="$ROOT/venv/bin/python"
fi

"$PY" -m pip install --upgrade pip uv
if [[ ! -f "$ROOT/requirements.lock" ]]; then
  "$PY" -m uv pip compile requirements.txt -o "$ROOT/requirements.lock"
fi
if [[ ! -f "$ROOT/requirements-dev.lock" ]]; then
  "$PY" -m uv pip compile requirements-dev.txt -o "$ROOT/requirements-dev.lock"
fi
"$PY" -m uv pip sync "$ROOT/requirements.lock"
"$PY" -m uv pip sync "$ROOT/requirements-dev.lock"
"$PY" -m pip install -e ".[dev]"

echo ""
echo "=== Done ==="
echo "  Activate venv: source venv/bin/activate"
echo "  Signing:       docs/SIGNING.md"
echo "  Go-live:       docs/GO_LIVE.md"
