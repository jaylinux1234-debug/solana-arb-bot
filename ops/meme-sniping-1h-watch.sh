#!/usr/bin/env bash
# Collect meme sniping sim logs for N minutes and write a summary report.
set -euo pipefail

DURATION_MIN="${1:-60}"
LOG_DIR="${LOG_DIR:-/opt/solana-arb-bot/logs}"
OUT="${OUT:-/tmp/meme-sniping-sim-report.txt}"
CONTAINER="${CONTAINER:-solana-arb-monitor}"
START_TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

echo "=== Meme Sniping Sim Watch ===" | tee "$OUT"
echo "Started: $START_TS UTC | Duration: ${DURATION_MIN} min" | tee -a "$OUT"
echo "Simulate flag: $(grep -E '^MEME_SNIPING_SIMULATE=' /opt/solana-arb-bot/.env 2>/dev/null || echo 'unknown')" | tee -a "$OUT"
echo "" | tee -a "$OUT"

END_EPOCH=$(( $(date +%s) + DURATION_MIN * 60 ))
while [ "$(date +%s)" -lt "$END_EPOCH" ]; do
  sleep 300
  echo "--- checkpoint $(date -u +"%H:%M:%S") ---" >> "$OUT"
  docker logs "$CONTAINER" 2>&1 | grep -E 'meme_sniping|meme_snipe|\[SIM\]' | tail -20 >> "$OUT" || true
  if [ -f "$LOG_DIR/meme_sniping_sim.jsonl" ]; then
    tail -1 "$LOG_DIR/meme_sniping_sim.jsonl" >> "$OUT" 2>/dev/null || true
  fi
done

echo "" | tee -a "$OUT"
echo "=== FINAL SUMMARY $(date -u +"%Y-%m-%dT%H:%M:%SZ") ===" | tee -a "$OUT"

docker logs "$CONTAINER" 2>&1 | grep -c 'meme_sniping_scan' | xargs -I{} echo "scan_log_lines: {}" | tee -a "$OUT"
docker logs "$CONTAINER" 2>&1 | grep -c 'meme_sniping_filter' | xargs -I{} echo "filter_reviews: {}" | tee -a "$OUT"
docker logs "$CONTAINER" 2>&1 | grep -c 'meme_sniping_strong_signal' | xargs -I{} echo "strong_signals: {}" | tee -a "$OUT"
docker logs "$CONTAINER" 2>&1 | grep -c '\[SIM\] meme_snipe v2' | xargs -I{} echo "sim_entries: {}" | tee -a "$OUT"
docker logs "$CONTAINER" 2>&1 | grep -c '\[SIM\] meme_snipe sell' | xargs -I{} echo "sim_exits: {}" | tee -a "$OUT"
docker logs "$CONTAINER" 2>&1 | grep -c 'meme_sniping_sim_summary' | xargs -I{} echo "periodic_summaries: {}" | tee -a "$OUT"
docker logs "$CONTAINER" 2>&1 | grep -c 'pump.fun API unavailable' | xargs -I{} echo "pump_fallback_warnings: {}" | tee -a "$OUT"

echo "" | tee -a "$OUT"
echo "--- reject reasons ---" | tee -a "$OUT"
docker logs "$CONTAINER" 2>&1 | grep 'meme_sniping_filter' | grep 'approved=False' | tail -30 >> "$OUT" || true

echo "" | tee -a "$OUT"
echo "--- sim trades ---" | tee -a "$OUT"
docker logs "$CONTAINER" 2>&1 | grep -E '\[SIM\] meme_snipe' | tail -40 >> "$OUT" || true

echo "" | tee -a "$OUT"
echo "--- last 5 periodic summaries ---" | tee -a "$OUT"
docker logs "$CONTAINER" 2>&1 | grep 'meme_sniping_sim_summary' | tail -5 >> "$OUT" || true

if [ -f "$LOG_DIR/meme_sniping_sim.jsonl" ]; then
  echo "" | tee -a "$OUT"
  echo "--- jsonl tail ---" | tee -a "$OUT"
  tail -10 "$LOG_DIR/meme_sniping_sim.jsonl" >> "$OUT" 2>/dev/null || true
fi

echo "" | tee -a "$OUT"
echo "Report written: $OUT" | tee -a "$OUT"
