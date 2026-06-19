#!/usr/bin/env bash
# Live spread / block monitor — run in dedicated SSH session.
# Usage: bash ops/monitor-spreads.sh
docker logs -f solana-arb-monitor --tail 100 2>&1 | grep --line-buffered -E 'gross|net|LIVE FILL|STRONG|blocked|cost_model|CYCLE_SPREAD'
