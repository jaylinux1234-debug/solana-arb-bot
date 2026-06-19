#!/usr/bin/env bash
# Dump current tuning .env keys to logs/ops for audit trail.
set -euo pipefail
cd /opt/solana-arb-bot
mkdir -p logs/ops
TS="$(date -u +%Y%m%d_%H%M%S)"
OUT="logs/ops/env_snapshot_${TS}.env"
grep -E '^[A-Z_]+=' .env | sort > "$OUT"
cp -f "$OUT" logs/ops/LATEST_ENV_SNAPSHOT.env
echo "Saved: $OUT"
echo "Latest: logs/ops/LATEST_ENV_SNAPSHOT.env"
