#!/bin/bash
# Blue-green deploy with MEV lane safety (backrun / collateral / liquidation).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ACTIVE_FILE="${BLUE_GREEN_ACTIVE_FILE:-logs/blue_green_active.txt}"
COMPOSE_FILES=(
  -f docker-compose.yml
  -f infra/compose/docker-compose.prod.yml
  -f infra/compose/docker-compose.prod.override.yml
)

BLUE_PORT="${MONITOR_BLUE_PORT:-8001}"
GREEN_PORT="${MONITOR_GREEN_PORT:-8002}"
HEALTH_WAIT_SEC="${BLUE_GREEN_HEALTH_WAIT_SEC:-45}"
MEV_WATCH_TAIL="${MEV_WATCH_TAIL:-30}"

health_ok() {
  local port="$1"
  curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1
}

mev_status() {
  local port="$1"
  curl -sf "http://127.0.0.1:${port}/mev/status" 2>/dev/null || true
}

profile_port() {
  if [[ "$1" == "blue" ]]; then
    echo "$BLUE_PORT"
  else
    echo "$GREEN_PORT"
  fi
}

echo "🚦 Starting Blue-Green Deploy with MEV safety..."

PROFILE="${1:-}"
if [[ -z "$PROFILE" ]]; then
  if health_ok "$BLUE_PORT"; then
    echo "Blue healthy on :${BLUE_PORT} → switching to Green"
    PROFILE=green
  elif health_ok "$GREEN_PORT"; then
    echo "Green healthy on :${GREEN_PORT} → switching to Blue"
    PROFILE=blue
  elif [[ -f "$ACTIVE_FILE" ]]; then
    CURRENT="$(tr -d '[:space:]' < "$ACTIVE_FILE")"
    if [[ "$CURRENT" == "blue" ]]; then
      PROFILE=green
    else
      PROFILE=blue
    fi
    echo "No health on :${BLUE_PORT}/:${GREEN_PORT} — toggle from active file → ${PROFILE}"
  else
    echo "Blue unhealthy on :${BLUE_PORT} → activating Green (cold start)"
    PROFILE=green
  fi
fi

if [[ "$PROFILE" != "blue" && "$PROFILE" != "green" ]]; then
  echo "Usage: $0 [blue|green]" >&2
  exit 1
fi

OTHER=blue
[[ "$PROFILE" == "blue" ]] && OTHER=green

CURRENT="$OTHER"
if [[ -f "$ACTIVE_FILE" ]]; then
  CURRENT="$(tr -d '[:space:]' < "$ACTIVE_FILE")"
fi

CURRENT_PORT="$(profile_port "$CURRENT")"
TARGET_PORT="$(profile_port "$PROFILE")"

if health_ok "$CURRENT_PORT"; then
  echo "✅ ${CURRENT} healthy on :${CURRENT_PORT} → deploying ${PROFILE}"
else
  echo "⚠️  ${CURRENT} unhealthy on :${CURRENT_PORT} → deploying ${PROFILE} (failover)"
fi

echo "==> MEV env (compose.env)"
grep -E '^(V2_ROUTER_MEV_ONLY|ENABLE_HELIUS_WEBHOOK|ENABLE_HELIUS_WEBHOOK_BACKRUN|ENABLE_COLLATERAL|ENABLE_LIQUIDATION|COLLATERAL_MIN_NET_BPS|LIQUIDATION_MIN_PROFIT_USDC)=' compose.env 2>/dev/null || true

echo "==> sync compose.env + build monitor-${PROFILE}"
npm run sync:compose-env

docker compose "${COMPOSE_FILES[@]}" --profile "$PROFILE" up -d --build "monitor-${PROFILE}" redis

echo "==> Waiting ${HEALTH_WAIT_SEC}s for monitor-${PROFILE} on :${TARGET_PORT}"
sleep "$HEALTH_WAIT_SEC"

for i in $(seq 1 30); do
  if health_ok "$TARGET_PORT"; then
    echo "✅ Health OK | http://127.0.0.1:${TARGET_PORT}/health"
    MEV_JSON="$(mev_status "$TARGET_PORT")"
    if [[ -n "$MEV_JSON" ]]; then
      echo "   MEV status: $MEV_JSON"
    fi
    break
  fi
  sleep 4
  if [[ "$i" -eq 30 ]]; then
    echo "❌ Health check failed for monitor-${PROFILE} on :${TARGET_PORT}" >&2
    exit 1
  fi
done

docker compose "${COMPOSE_FILES[@]}" --profile "$OTHER" stop "monitor-${OTHER}" 2>/dev/null || true

mkdir -p "$(dirname "$ACTIVE_FILE")"
echo "$PROFILE" > "$ACTIVE_FILE"

npm run metrics:next 2>/dev/null || true

echo "✅ Deploy complete. MEV lanes active on profile=${PROFILE}."
echo "   Health:  http://127.0.0.1:${TARGET_PORT}/health"
echo "   MEV:     http://127.0.0.1:${TARGET_PORT}/mev/status"
echo "   Webhook: http://127.0.0.1:$([[ "$PROFILE" == "blue" ]] && echo 8798 || echo 8797)/helius/webhook"
echo "   Watch:   npm run mev:watch:live --tail=${MEV_WATCH_TAIL}"

if [[ -f logs/v2_attempts.jsonl ]]; then
  echo ""
  echo "==> Recent MEV attempts (last ${MEV_WATCH_TAIL} matching lines)"
  tail -n 200 logs/v2_attempts.jsonl 2>/dev/null \
    | grep -iE 'backrun|collateral|liquidation|MEV|kamino' \
    | tail -n "$MEV_WATCH_TAIL" \
    || echo "(no recent MEV lines)"
fi

if command -v python >/dev/null 2>&1; then
  python scripts/mev_watch.py --summary-only 2>/dev/null || true
fi
