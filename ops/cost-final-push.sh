#!/bin/bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=== Final Cost Model Push for First Fills ==="

sed -i 's/V2_BASE_COST_BPS=.*/V2_BASE_COST_BPS=4.8/' .env
sed -i 's/V2_SLIPPAGE_BUFFER_BPS=.*/V2_SLIPPAGE_BUFFER_BPS=1.8/' .env
# Integer only — 5.5 crashes Pydantic (use 5 for ~5.5 intent)
sed -i 's/CEX_DEX_MIN_GROSS_SPREAD_BPS=.*/CEX_DEX_MIN_GROSS_SPREAD_BPS=5/' .env
sed -i 's/V2_ROUNDTRIP_SOFT_PASS_FACTOR=.*/V2_ROUNDTRIP_SOFT_PASS_FACTOR=0.88/' .env

echo "Updated:"
grep -E 'V2_BASE_COST_BPS|V2_SLIPPAGE_BUFFER_BPS|CEX_DEX_MIN_GROSS|V2_ROUNDTRIP_SOFT_PASS' .env

node scripts/clear-singleton-lock.mjs 2>/dev/null || true
npm run sync:compose-env
bash scripts/restart-monitor.sh

echo "✅ Final cost push applied"
