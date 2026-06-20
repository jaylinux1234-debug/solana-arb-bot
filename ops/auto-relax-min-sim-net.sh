#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE=".env"
LOG_FILE="logs/v2.log"
KEY="CEX_DEX_MODEL_NET_SOFT_RESCUE_MIN_SIM_NET_BPS"

STRICT_VALUE="${STRICT_VALUE:-0.8}"
RELAX_VALUE="${RELAX_VALUE:-0.5}"
WINDOW_SEC="${WINDOW_SEC:-300}"
DEEP_NEG_BPS="${DEEP_NEG_BPS:--10}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found in $ROOT_DIR"
  exit 1
fi

set_env_key() {
  local key="$1"
  local val="$2"
  sed -i "/^${key}=/d" "$ENV_FILE"
  echo "${key}=${val}" >> "$ENV_FILE"
}

get_env_key() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | cut -d '=' -f 2-
}

apply_and_restart() {
  local value="$1"
  set_env_key "$KEY" "$value"
  npm run sync:compose-env
  npm run compose:prod:restart:no-build
  npm run health:quick
}

echo "== Auto relax policy (strict) =="
echo "root:        $ROOT_DIR"
echo "key:         $KEY"
echo "strict:      $STRICT_VALUE"
echo "relax:       $RELAX_VALUE"
echo "window_sec:  $WINDOW_SEC"
echo "deep_neg_bps:$DEEP_NEG_BPS"

if [[ ! -f "$LOG_FILE" ]]; then
  echo "WARN: $LOG_FILE not found yet; creating an empty file for tail."
  mkdir -p "$(dirname "$LOG_FILE")"
  touch "$LOG_FILE"
fi

echo
echo "Applying relaxed value and restarting..."
apply_and_restart "$RELAX_VALUE"

echo
echo "Watching funnel for ${WINDOW_SEC}s..."

pass_count=0
opp_count=0
exec_count=0
fill_count=0
deep_neg_count=0
best_net="-9999"

while IFS= read -r line; do
  [[ -z "$line" ]] && continue

  if [[ "$line" == *"MODEL_NET_SOFT_RESCUE |"* ]]; then
    ((pass_count+=1))
  fi
  if [[ "$line" == *"OPPORTUNITY |"* ]]; then
    ((opp_count+=1))
  fi
  if [[ "$line" == *"EXECUTING |"* ]]; then
    ((exec_count+=1))
  fi
  if [[ "$line" == *"LIVE FILL"* ]]; then
    ((fill_count+=1))
  fi

  if [[ "$line" == *"ROUNDTRIP_PRE_SIM reject"* ]]; then
    net="$(echo "$line" | sed -n 's/.*net=\([-0-9.]*\)bps.*/\1/p')"
    if [[ -n "$net" ]]; then
      if awk -v n="$net" -v b="$best_net" 'BEGIN { exit !(n > b) }'; then
        best_net="$net"
      fi
      if awk -v n="$net" -v t="$DEEP_NEG_BPS" 'BEGIN { exit !(n <= t) }'; then
        ((deep_neg_count+=1))
      fi
    fi
  fi
done < <(timeout "${WINDOW_SEC}s" tail -n 0 -F "$LOG_FILE" || true)

echo
echo "== Auto relax summary =="
echo "pass_count:     $pass_count"
echo "opportunity:    $opp_count"
echo "executing:      $exec_count"
echo "live_fill:      $fill_count"
echo "deep_neg_count: $deep_neg_count"
echo "best_sim_net:   ${best_net}bps"

# Strict policy: keep relaxed only if there is strong evidence the funnel improved.
keep_relaxed=0
if (( fill_count > 0 )); then
  keep_relaxed=1
elif (( pass_count > 0 && opp_count > 0 )); then
  keep_relaxed=1
fi

if (( keep_relaxed == 1 )); then
  echo
  echo "Decision: KEEP relaxed value (${RELAX_VALUE})"
  current_val="$(get_env_key "$KEY")"
  echo "Current ${KEY}=${current_val}"
  exit 0
fi

echo
echo "Decision: REVERT to strict value (${STRICT_VALUE})"
apply_and_restart "$STRICT_VALUE"
current_val="$(get_env_key "$KEY")"
echo "Current ${KEY}=${current_val}"

if (( deep_neg_count > 0 )); then
  echo "Reason: rejects remain deep negative (<= ${DEEP_NEG_BPS}bps)."
else
  echo "Reason: no qualifying rescue pass/opportunity/fill during watch window."
fi
