#!/bin/bash
# Tier F Step 1 — baseline metrics
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== Tier F baseline ==="
npm run metrics:next
npm run probe:daily
npm run sim:breakeven:live
echo "Done. Next: npm run sync:compose-env && npm run compose:prod:up"
