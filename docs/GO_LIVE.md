# Go-live runbook

Run from repo root. On Windows, `:sh` scripts use Node wrappers (no Git Bash required).

## Quick start (Plan 1 cost model → prod)

Edit `.env` (then sync — never commit secrets):

```env
# Plan 1 — simplified cost model + small-account / inventory
CEX_DEX_USE_COMPONENT_COST_MODEL=false
CEX_DEX_STRATEGY_BASE_COST_BPS=14
GO_LIVE_SMALL_ACCOUNT=true
CEX_DEX_INVENTORY_FIRST=true
CEX_DEX_INVENTORY_BUFFER_FRAC=1.05
CEX_DEX_PROBE_USDC_MICRO=12000000
CEX_USE_ASK_FOR_BUY=true

# Live policy (required for real sends)
LIVE_TRADING_CONFIRM=YES
SIGNER_TYPE=ledger
LEDGER_SIGN_URL=http://host.docker.internal:8546/sign

# Plans 5–7 (optional tuning)
CEX_DEX_ORACLE_POLL_MIN_SEC=1
CEX_DEX_BRAIN_PRIORITY_BIAS=80
STRATEGY_PRIORITY_ORDER=cex_dex,backrun,collateral_swap,liquidation
CEX_DEX_MAX_TRADE_USDC_MICRO=12000000
JITO_TIP_LAMPORTS=100000
ENABLE_ONCHAIN_PROFIT_ASSERT=true
ONCHAIN_PROFIT_ASSERT_BPS=12
ONCHAIN_PROFIT_ASSERT_STRICT=true
```

**Command sequence** (repo root):

```bash
# 1. Apply Plan 1 env + sync
npm run sync:compose-env
npm run secrets:sync-local
npm run go-live:preflight:sh

# 2. Deploy
npm run compose:prod:up

# 3. Monitor (bash / Git Bash)
npm run logs:tail | grep -E "OPPORTUNITY|EXECUTING|live_fill|cost_bps"
```

**Windows PowerShell** (no `grep` pipe):

```powershell
npm run logs:tail 2>&1 | Select-String -Pattern "OPPORTUNITY|EXECUTING|live_fill|cost_bps"
```

**Daily measurement:**

```bash
python scripts/daily_gate_probe.py
npm run results:fill-rate
```

Ledger bridge on the host before deploy: `node scripts/ledger-sign-bridge.mjs` (port **8546**).

## Tier F — Capital alignment (SOL + USDC)

Fund **Backpack + on-chain** with both SOL and USDC, then baseline → deploy → auto-gates → reverse bootstrap if direction-starved.

Full runbook: **[docs/TIER_F_CAPITAL.md](TIER_F_CAPITAL.md)**

```bash
npm run tier:f:baseline
npm run sync:compose-env
npm run compose:prod:up
# Terminal A: npm run gates:auto
# Terminal B: npm run monitor:enhanced
```

## 1. Simulate first (24h+ recommended)

```bash
npm run go-live:secrets:sh   # once: secrets/ + compose.env
npm run go-live:simulate
```

Starts the prod-shaped stack with `SIMULATE=true` and `TEST_MODE=true` (no live sends).

## 2. Monitoring (Prometheus + Grafana)

```bash
npm run go-live:monitoring
# equivalent:
npm run compose:up:monitoring
```

Uses `compose.env` (from `.env` via `sync:compose-env`) and `docker-compose.monitoring.yml`.

| UI | URL |
|----|-----|
| **Grafana** | http://localhost:3000 (remote: `http://your-server:3000`) |
| **Prometheus** | http://localhost:9090 (remote: `http://your-server:9090`) |
| **Health** | http://localhost:8000/health |

Grafana login: `admin` / `GRAFANA_ADMIN_PASSWORD` from `.env` (default `admin`).

Full ops guide: **[docs/MONITORING.md](MONITORING.md)**

### Tail logs

```bash
npm run logs:tail
# or direct container name:
npm run logs:follow
docker logs -f solana-arb-monitor
```

## 3. Monitor simulation

```bash
npm run logs:tail:simulate
```

Health: `http://localhost:8000/health`

Stop simulation:

```bash
npm run compose:simulate:down
```

## 3. Final preflight

Ensures `.env` prod policy, Docker daemon, `LIVE_TRADING_CONFIRM=YES`, pytest, and Hardhat compile.

```bash
npm run go-live:preflight:sh
```

Strict secrets (fail if any `secrets/*` empty):

```bash
# PowerShell
$env:REQUIRE_POPULATED_SECRETS=1; npm run go-live:preflight:sh
```

## 5. Deploy contracts (optional)

Ledger + Gnosis Safe owner. Fill `LEDGER_DEPLOYER_ADDRESS`, `GNOSIS_SAFE_ADDRESS`, `BASESCAN_API_KEY` in `.env`.

```bash
npm run deploy:secure
```

## 6. Production launch

```bash
npm run compose:prod:up
npm run logs:tail
npm run watch:24h   # optional: health + errors every 15 min → logs/watch_24h_*.log
```

**First 24h:** [docs/UPTIME_24H.md](UPTIME_24H.md) — logs in `./logs/`, analysis in `./backtest_results/`.

`compose:prod:up` runs a **health gate** (`ps` + `logs monitor --tail 100`) if the stack is already running, then `docker compose up --wait` until healthchecks pass.

### Never restart without health checks

Before any restart:

```bash
npm run compose:ps
npm run compose:logs
# or combined:
npm run compose:health
```

Safe restart — **monitor only** (redis / Prometheus / Grafana stay up):

```bash
npm run compose:health
npm run compose:prod:restart
```

Runs health gate, then:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.prod.override.yml \
  up --build -d --force-recreate --wait monitor
```

Rebuild skip: `npm run compose:prod:restart:no-build`

### Docker Swarm (rolling updates)

Initialize swarm once (`docker swarm init`), then:

```bash
npm run swarm:deploy    # first deploy (rolling update_config on monitor)
npm run swarm:update    # health gate + rolling service update
```

Uses `docker-compose.swarm.yml` (`start-first`, `failure_action: rollback`).

Emergency override only: `COMPOSE_SKIP_HEALTH_GATE=1 npm run compose:prod:up`

### Long-running monitor restart wrapper

Keeps `monitor` + redis + Prometheus + Grafana up; recreates monitor if unhealthy every 5 minutes.

```bash
chmod +x restart-monitor.sh
./restart-monitor.sh
# or:
npm run restart:monitor
```

Logs: `logs/monitor_YYYYMMDD_HHMM.log` (container: `solana-arb-monitor`).

### systemd (Linux production host)

On the server (not Windows dev machine):

```bash
cd /path/to/solana-arb-bot
chmod +x restart-monitor.sh scripts/install-systemd-monitor.sh

# Default unit: solana-arb-monitor.service
sudo bash scripts/install-systemd-monitor.sh

# Legacy name (matches aave-flashloan-base docs):
sudo bash scripts/install-systemd-monitor.sh --name flash-monitor
```

Manual equivalent:

```bash
# Edit paths in deploy/systemd/solana-arb-monitor.service, then:
sudo cp deploy/systemd/solana-arb-monitor.service /etc/systemd/system/flash-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now flash-monitor.service
journalctl -u flash-monitor -f
```

Requires Docker running before start (`After=docker.service`).

Prod uses `SIMULATE=false` from `compose.env` (synced from `.env` via `npm run sync:compose-env`).

### Env file layout (single source of truth)

| File | Purpose |
|------|---------|
| `.env` | Your config (gitignored) |
| `.env.example` | Committed template only |
| `compose.env` | Auto-generated for Docker Compose |
| `.env.txt` | **Removed** — run `npm run env:migrate` if you still have one |
