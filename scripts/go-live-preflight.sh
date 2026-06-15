#!/usr/bin/env bash
# Final preflight before npm run compose:prod:up
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY=python3
[[ -f "$ROOT/venv/bin/python" ]] && PY="$ROOT/venv/bin/python"

echo "=== go-live:preflight ==="
npm run sync:compose-env
"$PY" scripts/validate_go_live_env.py
"$PY" scripts/go_live_preflight_checks.py
npm run test:py
npm run compile
echo ""
echo "=== Preflight OK ==="
echo "  Deploy (optional): npm run deploy:secure"
echo "  Launch:            npm run compose:prod:up"
