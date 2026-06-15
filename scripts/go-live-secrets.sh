#!/usr/bin/env bash
# Prod go-live: layout secrets, decrypt, validate Ledger policy, sync compose.env.
# Run from repo root:  npm run go-live:secrets:sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/secrets-manifest.sh
source "$ROOT/scripts/secrets-manifest.sh"

echo "=== go-live:secrets ==="

bash "$ROOT/scripts/setup-secrets.sh"
bash "$ROOT/scripts/decrypt-secrets.sh"

echo ""
echo "=== Validate prod signer policy ==="
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "ERROR: python not found" >&2
  exit 1
fi

if [[ -f "$ROOT/venv/Scripts/python.exe" ]]; then
  PY="$ROOT/venv/Scripts/python.exe"
elif [[ -f "$ROOT/venv/bin/python" ]]; then
  PY="$ROOT/venv/bin/python"
fi

"$PY" "$ROOT/scripts/validate_go_live_env.py"

echo ""
echo "=== Secret file check ==="
MISSING=0
for name in "${SECRET_FILES[@]}"; do
  path="$ROOT/secrets/$name"
  if [[ ! -s "$path" ]]; then
    echo "  WARN: secrets/$name is missing or empty" >&2
    MISSING=$((MISSING + 1))
  else
    echo "  ok secrets/$name"
  fi
done

if [[ -n "${REQUIRE_POPULATED_SECRETS:-}" ]] && [[ "$MISSING" -gt 0 ]]; then
  echo "ERROR: REQUIRE_POPULATED_SECRETS=1 and $MISSING secret(s) empty" >&2
  exit 1
fi

echo ""
echo "=== Sync compose.env ==="
npm run sync:compose-env

echo ""
echo "=== go-live:secrets complete ==="
if [[ "$MISSING" -gt 0 ]]; then
  echo "  Fill empty secrets/* before: npm run compose:prod:up"
else
  echo "  Next: npm run compose:prod:up"
fi
