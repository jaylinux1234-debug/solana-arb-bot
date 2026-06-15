#!/usr/bin/env bash
# Phase 3: audit → Base deploy → Basescan verify (DEPLOYER_PRIVATE_KEY — EVM only).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NETWORK="${1:-base-mainnet}"
MODULE="ignition/modules/ArbMonitorRegistry.ts"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -n "${PRIVATE_KEY:-}" || -n "${PRIVATE_KEY_CEX_DEX:-}" ]]; then
  echo "ERROR: Unset PRIVATE_KEY / PRIVATE_KEY_CEX_DEX before deploy:secure (use DEPLOYER_PRIVATE_KEY for Base only)." >&2
  exit 1
fi

if [[ -z "${DEPLOYER_PRIVATE_KEY:-}" && -z "${BASE_DEPLOYER_PRIVATE_KEY:-}" ]]; then
  echo "ERROR: Set DEPLOYER_PRIVATE_KEY for Base contract deploy." >&2
  exit 1
fi

OWNER="${GNOSIS_SAFE_ADDRESS:-${TIMELOCK_ADDRESS:-}}"
if [[ -z "$OWNER" ]]; then
  echo "ERROR: Set GNOSIS_SAFE_ADDRESS (preferred) or TIMELOCK_ADDRESS as contract owner." >&2
  exit 1
fi

if [[ ! -d node_modules ]]; then
  npm install
fi

echo "=== deploy:secure network=$NETWORK owner=$OWNER ==="

npm run audit:all

VERIFY_FLAG=()
if [[ -n "${BASESCAN_API_KEY:-}" || -n "${ETHERSCAN_API_KEY:-}" ]]; then
  VERIFY_FLAG=(--verify)
  echo "  Basescan verify enabled"
else
  echo "  WARN: set BASESCAN_API_KEY to auto-verify on deploy" >&2
fi

npx hardhat ignition deploy "$MODULE" --network "$NETWORK" "${VERIFY_FLAG[@]}"

echo ""
echo "Post-deploy:"
echo "  1. Confirm owner on Basescan is Gnosis Safe / timelock."
echo "  2. Transfer Ownable2Step to timelock if required."
echo "  3. Revoke deployer EOA roles on Safe policy."
