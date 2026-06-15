# logs/

Runtime logs (host-mounted in prod Docker). **Gitignored** — safe to analyze locally.

| File | Source |
|------|--------|
| `bot_YYYYMMDD.log` | Daily bot log (`setup_logging` in `src/main.py`) |
| `wallet_safety_state.json` | Live trade gates / drawdown |
| `monitor_*.log` | `restart-monitor.sh` docker log captures |

Quick checks:

```bash
npm run logs:errors
npm run logs:tail
```
