# Docker build

- **Dockerfile:** repository root — hardened multi-stage (`builder` + `runtime`).
- **Context:** repository root (`docker compose build` from project root).
- **`.dockerignore`:** repository root.

Runtime image includes:

- `curl` — `HEALTHCHECK` hits `http://127.0.0.1:8000/health`
- `sops` — in-container decrypt when `SECRETS_ENCRYPTION=sops`
- `scripts/docker_healthcheck.py` — compose prod healthcheck override
