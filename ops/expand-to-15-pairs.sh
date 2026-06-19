#!/bin/bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=== Expanding to 15 Pairs with Risk Controls ==="

PAIRS="SOL,BONK,WIF,POPCAT,MEW,JUP,DRIFT,BRETT,MOODENG,GIGA,PNUT,FARTCOIN,DOG,TURBO,MICH"
PROVEN="BONK,WIF,POPCAT,MEW,JUP,DRIFT,BRETT,MOODENG,GIGA,PNUT,FARTCOIN,DOG,TURBO,MICH"

sed -i "s/CEX_MIDCAPS=.*/CEX_MIDCAPS=${PAIRS}/" .env
sed -i 's/CEX_MAX_MIDCAPS=.*/CEX_MAX_MIDCAPS=15/' .env
sed -i "s/CEX_PROVEN_MIDCAPS=.*/CEX_PROVEN_MIDCAPS=${PROVEN}/" .env

# Risk controls (gross must be integer — 6 not 6.5)
sed -i 's/V2_MAX_FLASH_USDC=.*/V2_MAX_FLASH_USDC=38/' .env
sed -i 's/^MAX_FLASH_USDC=.*/MAX_FLASH_USDC=38/' .env
sed -i 's/CEX_DEX_MIN_GROSS_SPREAD_BPS=.*/CEX_DEX_MIN_GROSS_SPREAD_BPS=6/' .env
sed -i 's/CEX_DEX_VOL_GATE_ENABLED=.*/CEX_DEX_VOL_GATE_ENABLED=true/' .env
sed -i 's/V2_POLL_INTERVAL_SEC=.*/V2_POLL_INTERVAL_SEC=3.5/' .env

echo "Updated:"
grep -E '^CEX_MIDCAPS=|^CEX_MAX_MIDCAPS=|^CEX_PROVEN_MIDCAPS=|^V2_MAX_FLASH|^MAX_FLASH|^CEX_DEX_MIN_GROSS|^V2_POLL' .env

npm run sync:compose-env
node scripts/clear-singleton-lock.mjs 2>/dev/null || true
sudo systemctl restart solana-arb-monitor

echo "✅ Expanded to 15 pairs with risk controls"
echo "Max trade size kept at \$38"
