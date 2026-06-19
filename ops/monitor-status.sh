#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== Live Bot Status ==="
echo "Time: $(date)"

docker exec solana-arb-monitor python scripts/v2_wallet_balance.py

echo -e "\nRecent spreads:"
docker logs solana-arb-monitor --tail 80 2>&1 | grep -E 'gross|net|LIVE FILL|STRONG' | tail -12

echo -e "\nHealth:"
if command -v jq >/dev/null 2>&1; then
  curl -sf http://127.0.0.1:8000/health | jq
else
  curl -sf http://127.0.0.1:8000/health
fi
