#!/usr/bin/env bash
# Pin dependencies with uv: requirements.txt → requirements.lock
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv…"
  python -m pip install --upgrade uv
fi

echo "=== uv pip compile (prod) ==="
uv pip compile requirements.txt -o requirements.lock "$@"

if [[ -f requirements-dev.txt ]]; then
  echo ""
  echo "=== uv pip compile (dev) ==="
  uv pip compile requirements-dev.txt -o requirements-dev.lock "$@"
fi

echo ""
echo "Done. Install prod:  uv pip sync requirements.lock"
echo "      Install dev:   uv pip sync requirements-dev.lock"
