#!/usr/bin/env bash
# Encrypt secrets/* → secrets/encrypted/* using age or sops (SECRETS_ENCRYPTION in .env).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE=".env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing .env — copy from .env.example (secrets live in secrets/.local + encrypted/)." >&2
  exit 1
fi
if [[ -f ".env.txt" || -f ".env.txt.bak" ]]; then
  echo "Legacy .env.txt present — run: npm run secrets:migrate" >&2
  exit 1
fi

if ! grep -qE '^SECRETS_ENCRYPTION=(age|sops)' "$ENV_FILE" 2>/dev/null \
  && [[ "${SECRETS_ENCRYPTION:-none}" != "age" && "${SECRETS_ENCRYPTION:-none}" != "sops" ]]; then
  echo "Set SECRETS_ENCRYPTION=age or sops in .env first (or export SECRETS_ENCRYPTION=age)." >&2
  exit 1
fi

ENCRYPT_SECRETS=1 bash "$ROOT/scripts/setup-secrets.sh"
