#!/bin/bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=== Fixing Multi-Pair Scanning (Critical Fix) ==="

cp src/strategies/cex_dex_strategy.py "src/strategies/cex_dex_strategy.py.bak.$(date +%Y%m%d_%H%M%S)"

node scripts/clear-singleton-lock.mjs 2>/dev/null || true
bash scripts/restart-monitor.sh

echo "✅ Multi-pair scanning patched (deploy cex_dex_strategy.py first)"
