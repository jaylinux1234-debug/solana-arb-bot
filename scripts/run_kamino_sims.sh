#!/usr/bin/env bash
# Batch Kamino collateral sims (Linux/macOS). From repo root:
#   chmod +x scripts/run_kamino_sims.sh
#   ./scripts/run_kamino_sims.sh
#   PER_SIGNER=200 ROUNDS=5 ./scripts/run_kamino_sims.sh

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PER_SIGNER="${PER_SIGNER:-200}"
ROUNDS="${ROUNDS:-1}"

if [[ ! -d venv ]]; then
  python3 -m venv venv
fi
# shellcheck source=/dev/null
source venv/bin/activate
pip install -q -r requirements.txt

if [[ ! -f .env ]]; then
  echo "ERROR: .env missing. Run: cp .env.example .env" >&2
  exit 2
fi

export TEST_MODE=true
for ((r = 1; r <= ROUNDS; r++)); do
  echo "=== Round $r / $ROUNDS | primary | $PER_SIGNER sims ==="
  python cex_dex_sim_batch.py --count "$PER_SIGNER" --mode kamino_collateral --signer primary || true
  echo "=== Round $r / $ROUNDS | cex | $PER_SIGNER sims ==="
  python cex_dex_sim_batch.py --count "$PER_SIGNER" --mode kamino_collateral --signer cex || true
done

echo "Done. See successful_sim_count in logs/wallet_safety_state.json"
