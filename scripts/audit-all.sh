#!/usr/bin/env bash
# Static analysis before mainnet deploy: solhint + slither.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== audit:all ==="

echo "[0/3] hardhat compile"
npx hardhat compile

echo "[1/3] solhint"
npx solhint "contracts/**/*.sol"

echo "[2/3] slither"
if command -v slither >/dev/null 2>&1; then
  slither . --exclude-dependencies || true
else
  echo "  slither not installed - pip install slither-analyzer"
fi

echo "=== audit:all complete ==="
