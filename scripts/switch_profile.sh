#!/usr/bin/env bash
# Switch active .env from profiles/<name>.env and sync compose.env
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROFILE="${1:-accumulation}"
SRC="profiles/${PROFILE}.env"

if [[ ! -f "$SRC" ]]; then
  echo "Profile not found: $SRC" >&2
  echo "Available profiles:" >&2
  ls -1 profiles/*.env 2>/dev/null | sed 's|profiles/||;s|\.env||' >&2 || true
  exit 1
fi

cp "$SRC" .env
echo "Applied profile: $PROFILE -> .env"

if command -v npm >/dev/null 2>&1; then
  npm run sync:compose-env
  echo "Synced compose.env"
else
  echo "npm not found — run: npm run sync:compose-env" >&2
fi
