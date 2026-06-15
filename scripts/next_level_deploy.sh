#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "Deploying Next-Level Enhancements..."

# 1. Copy new files (Docker prod layout when /app exists)
if [ -f scripts/enhanced_monitor.py ]; then
  cp -v scripts/enhanced_monitor.py /app/scripts/ 2>/dev/null || true
fi

# 2. Sync env
npm run sync:compose-env

# 3. Rebuild & restart prod stack
npm run compose:prod:up

echo "Next-level features deployed."
npm run metrics:next || true
echo "Run: python scripts/enhanced_monitor.py"
