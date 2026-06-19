#!/bin/bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=== Applying Net Gate & Cost Model Tuning ==="

# Core fixes for near-misses
sed -i 's/CEX_DEX_MIN_NET_SPREAD_BPS=.*/CEX_DEX_MIN_NET_SPREAD_BPS=0.75/' .env
sed -i 's/V2_ROUNDTRIP_SOFT_PASS_FACTOR=.*/V2_ROUNDTRIP_SOFT_PASS_FACTOR=0.81/' .env
sed -i 's/V2_BASE_COST_BPS=.*/V2_BASE_COST_BPS=6.5/' .env
sed -i 's/V2_SLIPPAGE_BUFFER_BPS=.*/V2_SLIPPAGE_BUFFER_BPS=3.2/' .env

# Keep size reasonable
sed -i 's/V2_MAX_FLASH_USDC=.*/V2_MAX_FLASH_USDC=35/' .env

echo "Updated values:"
grep -E 'CEX_DEX_MIN_NET_SPREAD_BPS|V2_ROUNDTRIP_SOFT_PASS_FACTOR|V2_BASE_COST_BPS|V2_SLIPPAGE_BUFFER_BPS|V2_MAX_FLASH_USDC' .env

npm run sync:compose-env
sudo systemctl restart solana-arb-monitor

echo "✅ Net gate tuned — should allow more 8–20 bps gross opportunities"
