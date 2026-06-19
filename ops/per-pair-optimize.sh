#!/bin/bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=== Per-Pair Sizing Optimization ==="

# Higher size for strong pairs
sed -i 's/V2_MAX_FLASH_USDC=.*/V2_MAX_FLASH_USDC=42/' .env
sed -i 's/^MAX_FLASH_USDC=.*/MAX_FLASH_USDC=42/' .env

# Keep meme aggression
sed -i 's/V2_POLL_INTERVAL_SEC=.*/V2_POLL_INTERVAL_SEC=3.2/' .env

echo "Updated values:"
grep -E '^V2_MAX_FLASH_USDC=|^MAX_FLASH_USDC=|^V2_POLL_INTERVAL_SEC=' .env

npm run sync:compose-env
node scripts/clear-singleton-lock.mjs 2>/dev/null || true
sudo systemctl restart solana-arb-monitor

echo "✅ Per-Pair sizing updated (max \$42)"
