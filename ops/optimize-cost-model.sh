#!/bin/bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=== Major Cost Model Optimization ==="

# Core cost reductions
sed -i 's/V2_BASE_COST_BPS=.*/V2_BASE_COST_BPS=5.5/' .env
sed -i 's/V2_SLIPPAGE_BUFFER_BPS=.*/V2_SLIPPAGE_BUFFER_BPS=2.5/' .env
sed -i 's/V2_COST_SIZE_IMPACT_LINEAR_BPS=.*/V2_COST_SIZE_IMPACT_LINEAR_BPS=2.2/' .env

# Softer requirements
sed -i 's/CEX_DEX_MIN_NET_SPREAD_BPS=.*/CEX_DEX_MIN_NET_SPREAD_BPS=0.5/' .env
sed -i 's/V2_MIN_NET_BPS=.*/V2_MIN_NET_BPS=0.5/' .env
sed -i 's/V2_MIN_NET_BPS_BASE=.*/V2_MIN_NET_BPS_BASE=0.5/' .env
sed -i 's/V2_ROUNDTRIP_SOFT_PASS_FACTOR=.*/V2_ROUNDTRIP_SOFT_PASS_FACTOR=0.85/' .env

# Jito efficiency
sed -i 's/JITO_TIP_LAMPORTS=.*/JITO_TIP_LAMPORTS=55000/' .env

echo "Updated values:"
grep -E 'V2_BASE_COST|V2_SLIPPAGE_BUFFER|V2_COST_SIZE_IMPACT|CEX_DEX_MIN_NET|V2_MIN_NET|V2_ROUNDTRIP_SOFT_PASS|JITO_TIP_LAMPORTS' .env | sort -u

npm run sync:compose-env
node scripts/clear-singleton-lock.mjs 2>/dev/null || true
sudo systemctl restart solana-arb-monitor

echo "✅ Major cost model optimization applied"
