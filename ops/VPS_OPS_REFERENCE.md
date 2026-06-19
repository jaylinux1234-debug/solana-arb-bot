# VPS Ops Reference — Solana Arb Bot

> Auto-maintained reference for troubleshooting. Last updated by ops scripts.
> Host: `root@167.233.116.94` · Path: `/opt/solana-arb-bot`

---

## Environment

| Item | Value |
|------|-------|
| Host | `167.233.116.94` |
| Project path | `/opt/solana-arb-bot` |
| Wallet | `HabdC9GVP1kmEeUU1UHBcC7ez1TEVL1fUVVLXikiYxCZ` |
| Monitor container | `solana-arb-monitor` |
| Redis container | `bot-redis` |
| Health | `http://127.0.0.1:8000/health/detailed` |
| Webhook | `https://webhook.mysolproject.com/helius/health` |
| Cloudflare tunnel | `mysolproject-tunnel` (`d40c0cd1-8a1f-4ded-aa55-35339f8f9d42`) |
| Compose override | **Required:** `infra/compose/docker-compose.vps.override.yml` |

---

## Capital (stable baseline)

| Location | Typical |
|----------|---------|
| On-chain USDC | ~$154.60 |
| Backpack USDC | ~$41.39 |
| Backpack SOL | ~0.765 |
| On-chain SOL | ~1.36 |

**Note:** ~$107 USDC “drop” was inventory moves (Backpack→chain, USDC→SOL), not trading loss. **0 live fills** historically.

---

## Current tuning (aggressive net — applied 2026-06-16)

```
CEX_DEX_MIN_NET_SPREAD_BPS=0.6
V2_MIN_NET_BPS=0.6
V2_MIN_NET_BPS_BASE=0.6
V2_ROUNDTRIP_SOFT_PASS_FACTOR=0.83
V2_BASE_COST_BPS=6.2
V2_SLIPPAGE_BUFFER_BPS=3.0
V2_MAX_FLASH_USDC=35
MAX_FLASH_USDC=35
CEX_DEX_MIN_GROSS_SPREAD_BPS=7 (must be integer — 6.5/7.5 crash Pydantic)
V2_POLL_INTERVAL_SEC=3.4
```

**Pairs:** `SOL,BONK,WIF,POPCAT,JUP,MEW,DRIFT` (7)  
**CEX venues:** `backpack,bybit,okx,kucoin,bitget` (Bitget in .env, may not be wired in code)  
**Smart inventory:** ON (withdraw/replenish/SOL→USDC)

---

## Scan behavior (why no fills)

- **SOL reverse lane:** gross ~0–2 bps, net ~**−54 bps** → blocked by `gross_below_threshold` (needs 8 bps gross)
- **Meme near-misses:** gross 30–44 bps in `logs/v2_attempts.jsonl` → blocked by `net_below_threshold`
- **Cost model drag:** ~50+ bps on reverse path
- **v2 execution:** SOL-only on reverse; memes scan on CEX-DEX path

---

## Known issues & fixes

| Issue | Fix |
|-------|-----|
| `CEX_DEX_MIN_GROSS_SPREAD_BPS=6.5` | Use integer **6** or **7** only |
| Health `backpack_usdc: 0` | Fixed in code — thread-safe cache + V2 config; rebuild container |
| Health `max_flash_usdc: 12` | Sync `V2_MAX_FLASH_USDC`; settings now prefer V2 |
| `V2_MIN_NET` overrides `CEX_DEX_MIN_NET` | Keep both in sync when tuning |
| Singleton lock after restart | `node scripts/clear-singleton-lock.mjs` |
| Cloudflare 502 | Use `127.0.0.1:8799` not `localhost` in tunnel config |
| `jq` missing on VPS | Use `python3 -m json.tool` |
| SOPS decrypt warnings | Plaintext secrets overlay in use |
| systemd + docker race | Clear singleton before restart |
| `StrategyRouter stopped` | Check singleton / container restart loop |

---

## Essential commands

```bash
cd /opt/solana-arb-bot

# Full status capture (saves to logs/ops/)
bash ops/capture-status.sh

# Analysis report
bash ops/vps_analysis_report.sh

# Balances
docker exec solana-arb-monitor python scripts/v2_wallet_balance.py

# Health
curl -sf http://127.0.0.1:8000/health/detailed | python3 -m json.tool

# Live monitor (keep running in SSH session)
bash ops/monitor-spreads.sh

# Clear singleton before manual restart
node scripts/clear-singleton-lock.mjs

# Restart stack
sudo systemctl restart solana-arb-monitor
# or
bash scripts/restart-monitor.sh

# MEV / attempts
./venv/bin/python scripts/mev_watch.py --tail 50

# Multi-pair scan
docker exec solana-arb-monitor python -m src.scripts.multi_pair_scanner
```

---

## Tuning scripts (in ops/)

| Script | Purpose |
|--------|---------|
| `tune-net-gate.sh` | Moderate net gate (0.75, cost 6.5/3.2) |
| `aggressive-net-tune.sh` | Aggressive net (0.6, cost 6.2/3.0, soft pass 0.83) |

---

## Log files

| Path | Contents |
|------|----------|
| `logs/v2_attempts.jsonl` | All scan attempts, block reasons, gross/net bps |
| `logs/v2.log` | V2 cycle + CYCLE_SPREAD lines |
| `logs/trade_history.jsonl` | Live fills (empty so far) |
| `logs/capital_delta.jsonl` | Capital movement (may not exist yet) |
| `logs/ops/` | Timestamped status captures |

---

## Code fixes deployed (2026-06-16)

- `src/monitoring/health.py` — backpack balance cache, V2 max flash in health
- `src/monitoring/cex_health.py` — balance cache for health thread
- `src/v2/config.py` — CEX_DEX_MIN_NET priority over stale V2_MIN_NET_BPS_BASE
- `src/config/settings.py` — V2_MAX_FLASH_USDC → trading.max_flash_usdc
- `src/core/singleton_guard.py` — re-acquire on key expiry
- Rebuild required: `bash scripts/restart-monitor.sh`

---

## Webhook / tunnel

- Public URL: `https://webhook.mysolproject.com/helius/webhook`
- Local bind: `http://127.0.0.1:8799`
- Service: `systemctl status cloudflared`
- Config: `/opt/solana-arb-bot/config.yml`

---

## Block reason stats (historical)

From `v2_attempts.jsonl`: `gross_below_threshold` >> `not_cex_cheap` >> `net_below_threshold`  
Top gross near-misses: **44.5, 43.3, 42.2, 37.8…** bps (all net-negative)

---

## Next tuning levers (if still no fills)

1. Lower cost model: `V2_BASE_COST_BPS`, `V2_SLIPPAGE_BUFFER_BPS`, roundtrip soft pass
2. Lower gross floor for low vol: `V2_MIN_GROSS_BPS`, `CEX_DEX_MIN_GROSS_SPREAD_BPS`
3. Wire meme pairs into v2 reverse execution (currently SOL-only)
4. Per-pair sizing (not implemented — global cap only)
