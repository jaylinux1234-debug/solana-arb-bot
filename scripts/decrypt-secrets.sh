#!/usr/bin/env bash
# Decrypt secrets/encrypted/* → secrets/* for docker-compose.prod.yml file mounts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SECRETS_DIR="$ROOT/secrets/.local"
ENCRYPTED_DIR="$ROOT/secrets/encrypted"

SECRET_FILES=(
  private_key
  private_key_cex_dex
  jupiter_api_key
  openai_api_key
  backpack_secret
  helius_api_key
)

ENV_FILE=""
for candidate in .env .env.txt .ENV.txt; do
  [[ -f "$candidate" ]] && ENV_FILE="$candidate" && break
done

read_mode() {
  local v="${SECRETS_ENCRYPTION:-}"
  v="$(echo "$v" | tr '[:upper:]' '[:lower:]' | tr -d '\r')"
  if [[ -n "$v" ]]; then
    echo "$v"
    return
  fi
  if [[ -n "$ENV_FILE" ]]; then
    grep -E '^SECRETS_ENCRYPTION=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' | tr '[:upper:]' '[:lower:]' || true
  else
    echo "none"
  fi
}

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR" 2>/dev/null || true

MODE="$(read_mode)"
echo "=== Decrypt secrets (mode=$MODE) ==="

decrypt_age() {
  local name="$1"
  local base="${name%.txt}"
  local enc=""
  local out="$SECRETS_DIR/$name"
  local identity="${AGE_IDENTITY_FILE:-$SECRETS_DIR/age.identity}"
  if [[ -f "$ENCRYPTED_DIR/${base}.enc" ]]; then
    enc="$ENCRYPTED_DIR/${base}.enc"
  elif [[ -f "$ENCRYPTED_DIR/${name}.age" ]]; then
    enc="$ENCRYPTED_DIR/${name}.age"
  elif [[ -f "$ENCRYPTED_DIR/${base}.age" ]]; then
    enc="$ENCRYPTED_DIR/${base}.age"
  fi
  if [[ -z "$enc" || ! -f "$enc" ]]; then
    return 0
  fi
  if [[ ! -f "$identity" ]]; then
    echo "  ERROR: missing $identity for age decrypt" >&2
    return 1
  fi
  age -d -i "$identity" -o "$out" "$enc"
  chmod 600 "$out" 2>/dev/null || true
  echo "  decrypted $enc → $out"
}

decrypt_sops() {
  local name="$1"
  local enc="$ENCRYPTED_DIR/$name.enc.yaml"
  local out="$SECRETS_DIR/$name"
  if [[ ! -f "$enc" ]]; then
    return 0
  fi
  sops --decrypt --input-type binary --output-type binary "$enc" >"$out"
  chmod 600 "$out" 2>/dev/null || true
  echo "  decrypted $enc → $out"
}

case "$MODE" in
  age)
    command -v age >/dev/null || { echo "age not installed" >&2; exit 1; }
    for name in "${SECRET_FILES[@]}"; do
      decrypt_age "$name" || true
    done
    ;;
  sops)
    command -v sops >/dev/null || { echo "sops not installed" >&2; exit 1; }
    for name in "${SECRET_FILES[@]}"; do
      decrypt_sops "$name" || true
    done
    ;;
  none)
    echo "  SECRETS_ENCRYPTION=none — using plaintext secrets/ only."
    ;;
  *)
    echo "  Unknown SECRETS_ENCRYPTION=$MODE" >&2
    exit 1
    ;;
esac

echo "Done."
