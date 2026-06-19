#!/bin/bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=== Final Sizing Fixes ==="

# 1. Raise global cap so BONK/WIF can reach $42
sed -i 's/V2_MAX_FLASH_USDC=.*/V2_MAX_FLASH_USDC=42/' .env
sed -i 's/^MAX_FLASH_USDC=.*/MAX_FLASH_USDC=42/' .env

echo "Global caps:"
grep -E '^V2_MAX_FLASH_USDC=|^MAX_FLASH_USDC=' .env

npm run sync:compose-env
node scripts/clear-singleton-lock.mjs 2>/dev/null || true
bash scripts/restart-monitor.sh

echo "✅ Global cap raised + intelligent sizing added"
