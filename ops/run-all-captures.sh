#!/usr/bin/env bash
# Run all ops captures — call after tuning or when debugging.
set -euo pipefail
cd /opt/solana-arb-bot
bash ops/snapshot-env.sh
bash ops/capture-status.sh
bash ops/vps_analysis_report.sh > "logs/ops/analysis_$(date -u +%Y%m%d_%H%M%S).txt" 2>&1
echo "All ops artifacts under logs/ops/ — see ops/VPS_OPS_REFERENCE.md"
