#!/bin/bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=== Setting Real Per-Pair Sizing ==="

# Global fallback
sed -i 's/V2_MAX_FLASH_USDC=.*/V2_MAX_FLASH_USDC=35/' .env
sed -i 's/^MAX_FLASH_USDC=.*/MAX_FLASH_USDC=35/' .env

# Remove prior per-pair block if re-running
sed -i '/^# === PER-PAIR MAX TRADE SIZES/,/^PAIR_MAX_SIZE_MICH=/d' .env

cat << 'EOT' >> .env

# === PER-PAIR MAX TRADE SIZES (USDC) ===
PAIR_MAX_SIZE_BONK=42
PAIR_MAX_SIZE_WIF=42
PAIR_MAX_SIZE_POPCAT=32
PAIR_MAX_SIZE_MEW=28
PAIR_MAX_SIZE_JUP=35
PAIR_MAX_SIZE_DRIFT=30
PAIR_MAX_SIZE_BRETT=25
PAIR_MAX_SIZE_MOODENG=22
PAIR_MAX_SIZE_GIGA=25
PAIR_MAX_SIZE_PNUT=20
PAIR_MAX_SIZE_FARTCOIN=18
PAIR_MAX_SIZE_DOG=22
PAIR_MAX_SIZE_TURBO=20
PAIR_MAX_SIZE_MICH=18
EOT

echo "Per-pair caps:"
grep '^PAIR_MAX_SIZE_' .env
echo "Global: V2_MAX_FLASH_USDC=$(grep '^V2_MAX_FLASH_USDC=' .env)"

npm run sync:compose-env
node scripts/clear-singleton-lock.mjs 2>/dev/null || true
sudo systemctl restart solana-arb-monitor

echo "✅ Per-pair sizing applied"
