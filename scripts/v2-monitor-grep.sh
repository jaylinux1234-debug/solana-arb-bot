#!/bin/bash
# Persistent filtered tail for v2.log (run via nohup on VPS).
cd /opt/solana-arb-bot
PATTERN='CEX-DEX Scan|OPPORTUNITY|Thin book|LIVE FILL|BONK|WIF|POPCAT|MEW|gross_bps|net_bps'
exec tail -F logs/v2.log | grep --line-buffered -E "$PATTERN"
