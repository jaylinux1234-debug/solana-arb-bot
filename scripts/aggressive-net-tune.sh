#!/bin/bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=== Applying Stronger Net Gate Tuning ==="

sed -i 's/CEX_DEX_MIN_NET_SPREAD_BPS=.*/CEX_DEX_MIN_NET_SPREAD_BPS=0.6/' .env
sed -i 's/V2_MIN_NET_BPS=.*/V2_MIN_NET_BPS=0.6/' .env
sed -i 's/V2_MIN_NET_BPS_BASE=.*/V2_MIN_NET_BPS_BASE=0.6/' .env
sed -i 's/V2_ROUNDTRIP_SOFT_PASS_FACTOR=.*/V2_ROUNDTRIP_SOFT_PASS_FACTOR=0.83/' .env
sed -i 's/V2_BASE_COST_BPS=.*/V2_BASE_COST_BPS=6.2/' .env
sed -i 's/V2_SLIPPAGE_BUFFER_BPS=.*/V2_SLIPPAGE_BUFFER_BPS=3.0/' .env

echo "Updated values:"
grep -E 'CEX_DEX_MIN_NET|V2_MIN_NET|V2_ROUNDTRIP_SOFT_PASS|V2_BASE_COST|V2_SLIPPAGE_BUFFER' .env

npm run sync:compose-env
node scripts/clear-singleton-lock.mjs 2>/dev/null || true
sudo systemctl restart solana-arb-monitor

echo "✅ Aggressive net tuning applied — should allow more 12–35 bps gross trades"
