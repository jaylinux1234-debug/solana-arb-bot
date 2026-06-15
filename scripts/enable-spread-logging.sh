#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Enabling better spread logging..."

if grep -q '^CEX_DEX_LOG_NEAR_MISSES=' .env 2>/dev/null; then
  sed -i.bak 's/^CEX_DEX_LOG_NEAR_MISSES=.*/CEX_DEX_LOG_NEAR_MISSES=true/' .env
  rm -f .env.bak
else
  echo "CEX_DEX_LOG_NEAR_MISSES=true" >> .env
fi

echo "Done. Restarting monitor..."
npm run sync:compose-env
npm run compose:prod:restart
