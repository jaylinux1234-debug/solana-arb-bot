#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

echo "=== SOLANA ARB BOT LIVE VERIFICATION ==="

echo "1. Health Check:"
npm run health:quick

echo -e "\n2. Recent Logs (last 30 lines):"
docker logs solana-arb-monitor --tail 30

echo -e "\n3. Checking for key terms:"
docker logs solana-arb-monitor --tail 100 2>&1 | grep -E "quote_only|Ledger|signer|opportunity|AI approve|Backpack" || echo "No key terms found in last 100 lines"

echo -e "\n✅ Verification complete. Monitor with: npm run logs:tail"
