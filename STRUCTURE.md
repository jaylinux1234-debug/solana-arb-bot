# Project structure

```
solana-arb-bot/
├── src/
│   ├── config/          # Pydantic v2 settings
│   ├── core/            # wallet, rpc, security, circuit, ledger
│   ├── cex/
│   ├── dex/
│   ├── strategies/
│   ├── execution/
│   ├── monitoring/      # health server, metrics, logging (runtime)
│   └── utils/
├── infra/
│   ├── docker/          # .dockerignore reference, Docker notes
│   ├── compose/         # prod / monitoring / simulate / swarm overlays
│   └── monitoring/      # Prometheus, Grafana, Loki, Promtail configs
├── scripts/             # Bash + Node ops (compose, secrets, go-live)
├── secrets/
│   ├── encrypted/       # SOPS *.enc.yaml (Docker mounts)
│   ├── .local/          # plaintext sources for encrypt (gitignored)
│   └── README.md
├── tests/
├── deploy/              # systemd unit templates (optional)
├── pyproject.toml
├── package.json
├── Dockerfile           # build context: repo root
├── docker-compose.yml   # consolidated stack (monitor + redis + metrics)
├── compose.env          # synced from .env for Docker
└── .env.example
```

## Compose

- **Stack:** `docker-compose.yml` (consolidated: monitor, redis, prometheus, grafana, secrets, deploy limits)
- **Overlays:** `infra/compose/docker-compose.monitoring.yml` (Loki/Promtail), `.plaintext-secrets.yml` (auto), `.simulate.override.yml`, `.swarm.yml`
- **npm:** `compose-files.mjs` merges files; prefer `npm run compose:prod:up` over hand-typed `-f` lists.

## Secrets

1. Edit plaintext only under `secrets/.local/` (never commit).
2. `bash scripts/encrypt-secrets.sh` → `secrets/encrypted/*.enc.yaml`
3. Prod mounts encrypted files via `infra/compose/docker-compose.prod.yml`.
