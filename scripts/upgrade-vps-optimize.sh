#!/bin/bash
# VPS optimization: oracle poll, trade cap, dedupe .env (safe upsert)
set -euo pipefail

ROOT="/opt/solana-arb-bot"
cd "$ROOT"

upsert_env() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s/^${key}=.*/${key}=${value}/" .env
  else
    echo "${key}=${value}" >> .env
  fi
}

echo "=== VPS optimize: env tuning ==="

upsert_env CEX_DEX_ORACLE_POLL_MIN_SEC 1
upsert_env CEX_DEX_ORACLE_POLL_MAX_SEC 3
upsert_env CEX_DEX_MAX_TRADE_USDC_MICRO 42000000
upsert_env CEX_DEX_PARALLEL_SCAN true
upsert_env CEX_DEX_FOCUS_SCAN_SYMBOLS "SOL,BONK,WIF,POPCAT,MEW,PNUT"
upsert_env CEX_VOL_MAX_PCT 50

echo "=== Dedupe .env keys (keep last occurrence) ==="
python3 << 'PY'
from pathlib import Path

path = Path(".env")
lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
seen: set[str] = set()
out: list[str] = []
for line in reversed(lines):
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        key = stripped.split("=", 1)[0].strip()
        if key in seen:
            continue
        seen.add(key)
    out.append(line)
path.write_text("\n".join(reversed(out)) + "\n", encoding="utf-8")
print(f"Deduped .env ({len(lines)} -> {len(out)} lines)")
PY

grep -E '^(CEX_DEX_ORACLE_POLL|CEX_DEX_MAX_TRADE|CEX_DEX_PARALLEL|CEX_DEX_FOCUS|CEX_VOL_MAX)' .env || true

npm run sync:compose-env
node scripts/clear-singleton-lock.mjs || true
bash scripts/restart-monitor.sh

echo "=== Done ==="
