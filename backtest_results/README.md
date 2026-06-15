# backtest_results

## State snapshots

`npm run backup:state` writes daily dumps under `state_snapshots/YYYYMMDD/`:

- `redis_state.json` — Redis keys (prefix `bot:` by default)
- `wallet_safety_state.json`, `pnl_confidence_window.json` — copied from `logs/`

## Analysis outputs

Host-mounted directory for simulation exports and post-hoc analysis (`BACKTEST_RESULTS_DIR` in Docker).

Write batch outputs here from your backtest / sim scripts. Not used automatically by the live bot yet.

```bash
# Example layout
backtest_results/
  sim_20260522_cex_dex.json
  pnl_summary.csv
```
