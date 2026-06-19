#!/bin/bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=== Pushing for First Fills ==="

# Further reduce drag
sed -i 's/V2_BASE_COST_BPS=.*/V2_BASE_COST_BPS=5.2/' .env
sed -i 's/V2_SLIPPAGE_BUFFER_BPS=.*/V2_SLIPPAGE_BUFFER_BPS=2.2/' .env
sed -i 's/V2_COST_SIZE_IMPACT_LINEAR_BPS=.*/V2_COST_SIZE_IMPACT_LINEAR_BPS=2.0/' .env

# Lower gross floor for memes (integer only — 6 not 6.0)
sed -i 's/CEX_DEX_MIN_GROSS_SPREAD_BPS=.*/CEX_DEX_MIN_GROSS_SPREAD_BPS=6/' .env

# More lenient soft pass
sed -i 's/V2_ROUNDTRIP_SOFT_PASS_FACTOR=.*/V2_ROUNDTRIP_SOFT_PASS_FACTOR=0.87/' .env

echo "Updated values:"
grep -E 'V2_BASE_COST|V2_SLIPPAGE_BUFFER|V2_COST_SIZE_IMPACT|CEX_DEX_MIN_GROSS|V2_ROUNDTRIP_SOFT_PASS' .env | sort -u

npm run sync:compose-env
node scripts/clear-singleton-lock.mjs 2>/dev/null || true
sudo systemctl restart solana-arb-monitor

echo "✅ Cost model pushed harder — should allow more 8–15 bps gross trades"
