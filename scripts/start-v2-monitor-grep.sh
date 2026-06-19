#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
pkill -f 'scripts/v2-monitor-grep.sh' 2>/dev/null || true
sleep 1
nohup "$ROOT/scripts/v2-monitor-grep.sh" >> "$ROOT/logs/v2_monitor_filtered.log" 2>&1 &
echo "Monitor PID: $!"
grep -E 'CEX-DEX Scan|OPPORTUNITY|Thin book|LIVE FILL|BONK|WIF|POPCAT|MEW|gross_bps|net_bps' logs/v2.log | tail -8 >> logs/v2_monitor_filtered.log || true
echo "Recent lines in logs/v2_monitor_filtered.log:"
tail -10 logs/v2_monitor_filtered.log
