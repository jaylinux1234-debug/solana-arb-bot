#!/usr/bin/env bash
set -euo pipefail
cd /opt/solana-arb-bot

echo "=================================================="
echo "          PROJECT ANALYSIS REPORT"
echo "=================================================="

echo -e "\n1. Current Health & Status:"
curl -s http://127.0.0.1:8000/health/detailed | python3 -m json.tool 2>/dev/null || echo "Health endpoint not responding"

echo -e "\n2. Current Balances:"
docker exec solana-arb-monitor python scripts/v2_wallet_balance.py

echo -e "\n3. Active Configuration Summary:"
echo "CEX Venues     :" $(grep V2_CEX_VENUES .env)
echo "Pairs          :" $(grep CEX_MIDCAPS .env)
echo "Max Trade Size :" $(grep V2_MAX_FLASH_USDC .env)
echo "Poll Interval  :" $(grep V2_POLL_INTERVAL_SEC .env)
echo "Auto Inventory :" $(grep -E 'AUTO_WITHDRAW|REPLENISH|SWAP' .env | head -5)

echo -e "\n4. Recent Activity (Last 100 lines):"
docker logs solana-arb-monitor --tail 100 2>&1 | grep -E 'CYCLE|gross|net|LIVE FILL|blocked|STRONG' | tail -30 || true

echo -e "\n5. Recent Capital Movement:"
tail -10 logs/capital_delta.jsonl 2>/dev/null || echo "No capital delta log yet"

echo -e "\n6. Top Near-Misses:"
if [ -f logs/v2_attempts.jsonl ]; then
  python3 - <<'PY'
import json
from collections import Counter
gross = []
blocks = Counter()
fills = 0
with open("logs/v2_attempts.jsonl") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("gross_bps") is not None:
            gross.append(float(r["gross_bps"]))
        blocks[r.get("block_reason", "")] += 1
        if r.get("live_fill"):
            fills += 1
print("Top gross_bps:", sorted(gross, reverse=True)[:10])
print("Block reasons:", blocks.most_common(8))
print("Total live fills:", fills)
PY
else
  echo "No attempts log yet"
fi

echo -e "\n7. Container & Webhook:"
docker inspect -f 'monitor health={{.State.Health.Status}}' solana-arb-monitor 2>/dev/null || true
curl -sf https://webhook.mysolproject.com/helius/health && echo " webhook ok" || echo "webhook failed"
