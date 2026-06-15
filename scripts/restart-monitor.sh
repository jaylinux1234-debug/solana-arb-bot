#!/bin/bash
# =============================================================================
# Production Restart Wrapper - Smart, resilient, observable
# Usage: bash scripts/restart-monitor.sh   (or: npm run restart:monitor)
# =============================================================================

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOGFILE="$LOG_DIR/restart_${TIMESTAMP}.log"
CONTAINER="solana-arb-monitor"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"
}

log "=== SOLANA ARB MONITOR RESTART STARTED ==="

if command -v npm >/dev/null 2>&1; then
  npm run sync:compose-env --silent 2>/dev/null || npm run sync:compose-env || true
fi

COMPOSE=(
  docker compose
  --env-file compose.env
  -f docker-compose.yml
  -f infra/compose/docker-compose.prod.yml
  -f infra/compose/docker-compose.prod.override.yml
  -f infra/compose/docker-compose.monitoring.yml
)

missing_enc=0
for enc in \
  private_key.enc.yaml \
  private_key_cex_dex.enc.yaml \
  jupiter_api_key.enc.yaml \
  openai_api_key.enc.yaml \
  backpack_secret.enc.yaml \
  helius_api_key.enc.yaml \
  oneinch_api_key.txt.enc.yaml \
  cow_api_key.txt.enc.yaml \
  pagerduty_routing_key.txt.enc.yaml; do
  if [[ ! -f "secrets/encrypted/${enc}" ]]; then
    missing_enc=1
    break
  fi
done

if [[ "$missing_enc" == 1 && -f infra/compose/docker-compose.plaintext-secrets.yml ]]; then
  COMPOSE+=(-f infra/compose/docker-compose.plaintext-secrets.yml)
  log "Using plaintext secrets overlay (secrets/encrypted/*.enc.yaml missing)"
fi

if ! "${COMPOSE[@]}" config --quiet 2>>"$LOGFILE"; then
  log "Compose validation failed"
  exit 1
fi

log "Clearing stale Redis singleton lock (if present)..."
node scripts/clear-singleton-lock.mjs 2>/dev/null || true

log "Restarting services..."
"${COMPOSE[@]}" up --build -d --remove-orphans 2>&1 | tee -a "$LOGFILE"

MAX_WAIT=120
healthy=0
for ((elapsed=5; elapsed <= MAX_WAIT; elapsed += 5)); do
  status="$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "")"
  if [[ "$status" == "healthy" ]]; then
    log "Monitor is healthy (${elapsed}s)"
    healthy=1
    break
  fi
  log "Waiting for health... (${elapsed}s, status=${status:-unknown})"
  sleep 5
done

if [[ "$healthy" != 1 ]]; then
  log "Monitor did not become healthy within ${MAX_WAIT}s"
  docker inspect -f '{{json .State.Health}}' "$CONTAINER" 2>/dev/null | tee -a "$LOGFILE" || true
  exit 1
fi

log "Tailing logs in background → $LOGFILE"
docker logs -f "$CONTAINER" --tail 100 >>"$LOGFILE" 2>&1 &

log "Restart completed successfully. Log: $LOGFILE"
log "Follow: npm run logs:tail  |  npm run compose:logs"
