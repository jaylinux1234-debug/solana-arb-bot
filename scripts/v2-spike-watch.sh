#!/bin/bash
# Tail v2.log and highlight high-edge / fill events (run on VPS)
set -euo pipefail

ROOT="/opt/solana-arb-bot"
cd "$ROOT"
LOG="${1:-logs/v2.log}"

echo "Watching $LOG for spikes (Ctrl+C to stop)..."
tail -F "$LOG" | grep --line-buffered -E \
  'max_gross=[2-9][0-9]|max_gross=[1-9][0-9]{2,}|probe_edge=[2-9][0-9]|OPPORTUNITY|LIVE FILL|Scan order|Vol gate pass'
