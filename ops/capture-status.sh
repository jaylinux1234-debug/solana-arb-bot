#!/usr/bin/env bash
# Capture full VPS status snapshot for later troubleshooting.
set -euo pipefail

ROOT="/opt/solana-arb-bot"
cd "$ROOT"
mkdir -p logs/ops

TS="$(date -u +%Y%m%d_%H%M%S)"
OUT="logs/ops/status_${TS}.txt"
JSON="logs/ops/status_${TS}.json"

log() { echo "$@" | tee -a "$OUT"; }

log "============================================================"
log "  VPS STATUS CAPTURE — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
log "============================================================"
log ""

log "--- Container ---"
docker ps --filter name=solana-arb-monitor --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>&1 | tee -a "$OUT" || true
log ""

log "--- Health /health/detailed ---"
curl -sf http://127.0.0.1:8000/health/detailed 2>/dev/null | python3 -m json.tool >> "$OUT" 2>&1 || log "(health not responding)"
log ""

log "--- Balances ---"
docker exec solana-arb-monitor python scripts/v2_wallet_balance.py 2>&1 | tee -a "$OUT" || true
log ""

log "--- Active .env (tuning keys) ---"
grep -E '^V2_|^CEX_DEX_MIN|^CEX_MIDCAPS|^V2_CEX_VENUES|^MAX_FLASH|^AUTO_|^ENABLE_BACKPACK|^JITO_' .env 2>/dev/null | tee -a "$OUT" || true
log ""

log "--- Recent CYCLE_SPREAD (last 15) ---"
grep CYCLE_SPREAD logs/v2.log 2>/dev/null | tail -15 | tee -a "$OUT" || docker logs solana-arb-monitor --tail 500 2>&1 | grep CYCLE_SPREAD | tail -15 | tee -a "$OUT" || true
log ""

log "--- Block reasons (last 500 attempts) ---"
if [ -f logs/v2_attempts.jsonl ]; then
  tail -500 logs/v2_attempts.jsonl | python3 -c "
import sys, json
from collections import Counter
blocks = Counter()
gross = []
fills = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    blocks[r.get('block_reason', '')] += 1
    if r.get('gross_bps') is not None:
        gross.append(float(r['gross_bps']))
    if r.get('live_fill'):
        fills += 1
print('Block reasons:', blocks.most_common(10))
print('Live fills:', fills)
print('Top gross_bps:', sorted(gross, reverse=True)[:10])
" 2>&1 | tee -a "$OUT"
else
  log "(no v2_attempts.jsonl)"
fi
log ""

log "--- Singleton lock ---"
docker exec bot-redis redis-cli GET bot:singleton:v2 2>&1 | tee -a "$OUT" || true
log ""

log "--- Webhook ---"
curl -sf https://webhook.mysolproject.com/helius/health 2>&1 | tee -a "$OUT" || log "(webhook failed)"
log ""

log "--- systemd ---"
systemctl is-active solana-arb-monitor 2>&1 | tee -a "$OUT" || true
systemctl is-active cloudflared 2>&1 | tee -a "$OUT" || true
log ""

log "--- Recent errors (last 30) ---"
docker logs solana-arb-monitor --tail 300 2>&1 | grep -iE 'ERROR|singleton|stopped|429' | tail -30 | tee -a "$OUT" || true
log ""

log "Saved: $OUT"
cp -f "$OUT" logs/ops/LATEST_STATUS.txt 2>/dev/null || true
HEALTH_JSON="$(curl -sf http://127.0.0.1:8000/health/detailed 2>/dev/null || echo '{}')"
echo "$HEALTH_JSON" | python3 -m json.tool > "$JSON" 2>/dev/null || echo "$HEALTH_JSON" > "$JSON"
cp -f "$JSON" logs/ops/LATEST_STATUS.json 2>/dev/null || true
echo "Latest copies: logs/ops/LATEST_STATUS.txt logs/ops/LATEST_STATUS.json"
