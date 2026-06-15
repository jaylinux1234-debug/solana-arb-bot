#!/bin/sh
# Solana arb bot — Docker entrypoint (hot wallet only, Ledger removed).
set -e

echo "=== Solana Arb Bot Docker Entry Point (Hot Wallet Only) ==="

if [ "${SKIP_SECRET_VALIDATION:-false}" != "true" ]; then
  python -c "
from src.core.signer import HotWalletSigner
print('Validating hot wallet...')
HotWalletSigner.get_keypair()
print('Secrets & signer OK')
" || {
    echo "Secret validation failed"
    exit 1
  }
fi

if [ "${ENABLE_USDC_WITHDRAW_ON_START:-true}" = "true" ]; then
  echo "Auto-withdrawing USDC to chain..."
  python scripts/v2_withdraw_usdc.py --execute \
    --amount "${V2_USDC_WITHDRAW_ON_START_AMOUNT:-35}" \
    || echo "Warning: USDC withdraw failed (non-critical)"
fi

echo "Initializing core modules..."
python -c "
from src.dex.jupiter import JupiterExecutor
from src.cex.backpack import BackpackClient
from src.utils.sim import roundtrip_simulator
from src.brain.ml_brain import ai_approve_opportunity, route_strategy
print('Jupiter, Backpack, Simulator & ML brain loaded')
" || {
  echo "Core module load failed"
  exit 1
}

if [ "${ENABLE_USDC_SYNC_ON_START:-true}" = "true" ]; then
  echo "Syncing USDC inventory..."
  python scripts/usdc_inventory_sync.py --force \
    || echo "Warning: inventory sync failed (non-critical)"
fi

if [ "${ENABLE_AUTO_TUNER_ON_START:-false}" = "true" ]; then
  python scripts/auto_tuner.py || true
fi

if [ "${ENABLE_FILL_MODE_TUNER_ON_START:-false}" = "true" ]; then
  python scripts/auto_tuner.py --fill-mode --apply || true
fi

exec "$@"
