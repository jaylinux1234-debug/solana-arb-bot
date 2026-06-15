# solana-bot

Solana CEX-DEX arbitrage bot with collateral swap, liquidation monitoring, and production Docker stack.

## Layout

See **[STRUCTURE.md](STRUCTURE.md)** for the canonical tree. Summary:

```
src/           # application code (config, core, cex, dex, strategies, …)
infra/         # docker/, compose/, monitoring/
scripts/       # ops (compose, secrets, go-live)
secrets/       # encrypted/*.enc.yaml + .local/ (gitignored plaintext)
tests/
docker-compose.yml   # base stack; overlays in infra/compose/
```

## Focused v2 (SOL reverse only)

Simpler bot for dex-cheap reverse lane: **`docs/V2.md`** · `npm run v2:start`

## Quick start

Full install + signing steps: **[docs/SETUP.md](docs/SETUP.md)**

```bash
npm run setup:install              # npm install + pip requirements*
cp .env.example .env             # fill locally; never commit
npm run go-live:secrets:sh       # secrets/ + .env.txt prod policy
python -m src.main               # or: npm run compose:prod:up
```

## Signing (production)

- **`SIGNER_TYPE=hot`** with SOPS-encrypted `private_key` — no inline `PRIVATE_KEY` in prod env.
- **`ALLOW_HOT_KEY_IN_PROD=0`** — see [docs/SIGNING.md](docs/SIGNING.md).

```bash
npm run secrets:encrypt
npm run sync:compose-env
```

## Phase 3: Base contract audit & deploy (optional)

```bash
export DEPLOYER_PRIVATE_KEY=0x...              # Base EVM only — not Solana key
export GNOSIS_SAFE_ADDRESS=0xYourSafe
export BASESCAN_API_KEY=your_basescan_key
npm run audit:all
npm run deploy:secure
```

Details: [docs/PHASE3_DEPLOY.md](docs/PHASE3_DEPLOY.md).

## Docker

```bash
npm run sync:compose-env
docker compose up -d --build
npm run compose:prod:up    # sync env + prod stack (Phase 5)
npm run compose:prod:down
```

Prometheus: `rpc_connection_status{provider="alchemy"}`, `rpc_provider_failures_total`.

Monitoring & logs: **[docs/MONITORING.md](docs/MONITORING.md)** — `npm run logs:tail`, Grafana :3000, Prometheus :9090.
RPC CRITICAL alerts: `python.alerts.dispatch_alert` on 429 / WebSocket failures (logged; use Grafana/PagerDuty for paging).

Health API (compose default):

```bash
curl http://127.0.0.1:8000/health
```

## Development

- Prefer imports under `src.*` (e.g. `from src.dex.jupiter import JupiterExecutor`).
- Root `*.py` shims re-export `src` for legacy scripts; new code should use `src` only.
