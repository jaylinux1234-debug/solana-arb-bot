# First 24 hours — uptime checklist

## Start

```bash
npm run sync:compose-env
npm run compose:prod:up
npm run health:quick
```

Stack: **monitor** (bot) + **redis** + **Prometheus** + **Grafana** with `restart: unless-stopped`.

## What restarts automatically

| Layer | Behavior |
|-------|----------|
| **Docker** | `unless-stopped` on monitor, redis, prometheus, grafana |
| **CEX-DEX loop** | `run_forever()` catches errors; extra sleep on **429 / rate limit** |
| **Process** | `src.main` outer loop restarts after fatal crash (`BOT_RESTART_DELAY_SEC`, default 5s) |
| **Base WS** (`monitor_main`) | Provider failover, `safe_ws_call` retries, circuit breaker, 5s reconnect loop |
| **systemd** (Linux) | `restart-monitor.sh` / `flash-monitor.service` optional |

## Logs on disk (host)

| Path | Contents |
|------|----------|
| `./logs/bot_YYYYMMDD.log` | Structured daily bot log |
| `./logs/wallet_safety_state.json` | Safety / volume state |
| `./backtest_results/` | Sim / backtest exports (manual) |
| `docker logs` | `npm run logs:tail` |

## Watch during day 1

Every few hours:

```bash
npm run health:quick
npm run compose:ps:monitoring
npm run logs:errors
```

Optional watcher (health + errors snapshot every 15 min):

```bash
npm run watch:24h
```

## If something breaks

```bash
npm run logs:errors
npm run compose:restart:monitor   # quick
npm run compose:prod:restart      # rebuild + recreate monitor
```

Paste output of `npm run logs:errors` when asking for help — include timestamp and last 50 lines of `logs/bot_*.log`.

## URLs

- Health: http://localhost:8000/health  
- Grafana: http://localhost:3000  
- Prometheus: http://localhost:9090  

## After 24h

1. Archive `logs/` and `backtest_results/`
2. Review Grafana RPC panels (`rpc_connection_status`, `rpc_provider_failures_total`)
3. Tune `RPC_RATE_PER_SEC`, `WS_CONNECT_*` in `.env` if you saw 429 storms
