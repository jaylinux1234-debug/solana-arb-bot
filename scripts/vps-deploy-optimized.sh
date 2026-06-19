#!/usr/bin/env bash
set -euo pipefail

cd /opt/solana-arb-bot

echo "=== 1) git fetch + reset ==="
git fetch --all
git reset --hard origin/main

if [[ -f /tmp/deploy-bundle.tgz ]]; then
  echo "=== 2) unpack optimized code bundle ==="
  tar xzf /tmp/deploy-bundle.tgz -C /opt/solana-arb-bot
fi

cp -f scripts/replay_exec_ladder_uplift.py replay_exec_ladder_uplift.py 2>/dev/null || true
chmod +x harden-vps-baseline.sh ops/harden-vps-baseline.sh 2>/dev/null || true

echo "=== 3) env backups ==="
cp .env ".env.bak.$(date +%Y%m%d_%H%M%S)" || true
cp compose.env "compose.env.bak.$(date +%Y%m%d_%H%M%S)" || true

upsert_env() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${val}|" .env
  else
    echo "${key}=${val}" >> .env
  fi
}

upsert_env CEX_DEX_EXEC_SCAN_TOP_N 3
upsert_env CEX_DEX_EXEC_SIZE_LADDER "1.0,0.8,0.6,0.45,0.3"
upsert_env CEX_DEX_DYNAMIC_MIN_TRADE_PER_BPS_MICRO 450000
upsert_env CEX_DEX_DYNAMIC_MIN_TRADE_CAP_MICRO 7000000
upsert_env CEX_DEX_MODEL_NET_SOFT_RESCUE true
upsert_env CEX_DEX_MODEL_NET_SOFT_RESCUE_MIN_GROSS_BPS 8
upsert_env CEX_DEX_ORACLE_POLL_MIN_SEC 1
upsert_env CEX_DEX_ORACLE_POLL_MAX_SEC 3
upsert_env CEX_DEX_FOCUS_SCAN_SYMBOLS "SOL,BONK,WIF,POPCAT,MEW,PNUT"
upsert_env CEX_DEX_PARALLEL_SCAN true

echo "=== 4) harden ==="
sudo bash harden-vps-baseline.sh

echo "=== 5) sync compose env ==="
npm run sync:compose-env

echo "=== 6) docker pull + recreate ==="
docker compose -f docker-compose.yml -f docker-compose.security.override.yml pull
docker compose -f docker-compose.yml -f docker-compose.security.override.yml up -d --force-recreate --build

echo "=== waiting for health ==="
for i in $(seq 1 36); do
  status="$(docker inspect -f '{{.State.Health.Status}}' solana-arb-monitor 2>/dev/null || echo starting)"
  if [[ "$status" == "healthy" ]]; then
    echo "monitor healthy"
    break
  fi
  sleep 5
done

echo "=== 7) verify ==="
docker ps
echo "--- logs ---"
docker logs --tail 120 solana-arb-monitor 2>&1 | tail -40
echo "--- localhost health ---"
curl -sf http://127.0.0.1:8000/health || echo "health FAILED"
echo ""
echo "--- metrics ---"
curl -I http://127.0.0.1:9091/metrics 2>&1 | head -5
echo "--- public health (expect fail) ---"
curl -I --connect-timeout 3 http://167.233.116.94:8000/health 2>&1 | head -3 || true
echo "--- ufw ---"
sudo ufw status verbose
echo "--- optimization env ---"
docker exec solana-arb-monitor printenv | grep -E "CEX_DEX_EXEC_SCAN_TOP_N|CEX_DEX_EXEC_SIZE_LADDER|CEX_DEX_DYNAMIC_MIN_TRADE_PER_BPS_MICRO|CEX_DEX_DYNAMIC_MIN_TRADE_CAP_MICRO|CEX_DEX_MODEL_NET_SOFT_RESCUE|CEX_DEX_MODEL_NET_SOFT_RESCUE_MIN_GROSS_BPS|CEX_DEX_ORACLE_POLL_MIN_SEC|CEX_DEX_ORACLE_POLL_MAX_SEC|CEX_DEX_MIN_TRADE_USDC_MICRO|CEX_DEX_EXECUTION_SLIPPAGE_BUFFER_BPS" || true

echo "=== 8) replay script ==="
cd /opt/solana-arb-bot/logs
python3 ../replay_exec_ladder_uplift.py --log v2.log --fallback-log bot.log --rows 5000 --min-net 1.2 2>&1 | tail -30

echo "=== 9) live log grep ==="
grep -E "CEX-DEX Scan|MODEL_NET_SOFT_RESCUE|OPPORTUNITY|LIVE FILL|NEAR_MISS" v2.log | tail -n 80 | tail -20

echo "=== DONE ==="
