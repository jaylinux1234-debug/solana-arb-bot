#!/bin/bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=== Final Meme Performance Boost ==="

# Even more aggressive for memes (CEX_DEX_MIN_GROSS must be integer — 6 not 6.5)
sed -i 's/V2_POLL_INTERVAL_SEC=.*/V2_POLL_INTERVAL_SEC=3.0/' .env
sed -i 's/CEX_DEX_MIN_GROSS_SPREAD_BPS=.*/CEX_DEX_MIN_GROSS_SPREAD_BPS=6/' .env
sed -i 's/V2_ROUNDTRIP_SOFT_PASS_FACTOR=.*/V2_ROUNDTRIP_SOFT_PASS_FACTOR=0.86/' .env

echo "Updated values:"
grep -E '^V2_POLL_INTERVAL_SEC=|^CEX_DEX_MIN_GROSS_SPREAD_BPS=|^V2_ROUNDTRIP_SOFT_PASS_FACTOR=' .env

npm run sync:compose-env
node scripts/clear-singleton-lock.mjs 2>/dev/null || true
sudo systemctl restart solana-arb-monitor

echo "✅ Final meme boost applied (gross min=6 bps integer-safe)"
