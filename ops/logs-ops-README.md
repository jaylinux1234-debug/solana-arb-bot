# Ops artifacts — timestamped snapshots for debugging

Run captures after tuning or when investigating issues:

```bash
cd /opt/solana-arb-bot
bash ops/run-all-captures.sh
```

## Files

| File | Description |
|------|-------------|
| `LATEST_STATUS.txt` | Most recent full status capture |
| `LATEST_STATUS.json` | Health endpoint JSON |
| `LATEST_ENV_SNAPSHOT.env` | Sorted copy of all .env keys |
| `status_YYYYMMDD_HHMMSS.txt` | Timestamped status |
| `env_snapshot_*.env` | Timestamped env |
| `analysis_latest.txt` | Full analysis report |

## Reference

See `/opt/solana-arb-bot/ops/VPS_OPS_REFERENCE.md` for runbook, known issues, and commands.
