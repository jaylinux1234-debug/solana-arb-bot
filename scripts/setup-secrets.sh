#!/usr/bin/env bash
# Setup production secret files + prod safety flags (.env).
# Run from repo root:  bash scripts/setup-secrets.sh
#
# Does NOT generate or print private keys. Populate ./secrets/* yourself
# (Ledger export, vault, or rotate_secrets.sh for test keys only).
#
# Encryption at rest (optional):
#   SECRETS_ENCRYPTION=age   — age -R secrets/age-recipients.txt -o secrets/encrypted/*.enc
#   SECRETS_ENCRYPTION=sops  — Mozilla SOPS + .sops.yaml → secrets/encrypted/*.enc.yaml
#   ENCRYPT_SECRETS=1        — encrypt non-empty secrets/* when setup finishes
#   Or: bash scripts/encrypt-secrets.sh
#
# Before docker compose prod:
#   bash scripts/decrypt-secrets.sh   # materialize secrets/* from secrets/encrypted/

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SECRETS_DIR="$ROOT/secrets/.local"
ENCRYPTED_DIR="$ROOT/secrets/encrypted"
mkdir -p "$SECRETS_DIR" "$ENCRYPTED_DIR"
chmod 700 "$ROOT/secrets" "$SECRETS_DIR" "$ENCRYPTED_DIR" 2>/dev/null || true

# shellcheck source=scripts/secrets-manifest.sh
source "$ROOT/scripts/secrets-manifest.sh"

# --- Resolve env file (.env canonical; legacy .env.txt → npm run env:migrate) ---
ENV_FILE=""
for candidate in .env .env.txt .ENV.txt; do
  if [[ -f "$candidate" ]]; then
    ENV_FILE="$candidate"
    break
  fi
done
if [[ -z "$ENV_FILE" ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    ENV_FILE=".env"
    echo "Created .env from .env.example"
  else
    ENV_FILE=".env"
    touch "$ENV_FILE"
    echo "Created empty $ENV_FILE — fill non-secret settings."
  fi
fi
if [[ "$ENV_FILE" == ".env.txt" || "$ENV_FILE" == ".ENV.txt" ]]; then
  echo "WARN: $ENV_FILE is deprecated — run: npm run env:migrate" >&2
fi
ENV_TXT="$ENV_FILE"

append_kv_if_missing() {
  local line="$1"
  local key="${line%%=*}"
  if grep -qE "^${key}=" "$ENV_TXT" 2>/dev/null; then
    if grep -qF "$line" "$ENV_TXT"; then
      return 0
    fi
    echo "WARN: $ENV_TXT already has ${key}=... (not ${line}); edit manually if needed." >&2
    return 0
  fi
  echo "$line" >>"$ENV_TXT"
  echo "  + $line → $ENV_TXT"
}

ensure_secret_file() {
  local path="$1"
  local hint="$2"
  if [[ -s "$path" ]]; then
    echo "  ok $path (exists, non-empty)"
    return 0
  fi
  if [[ -f "$path" ]]; then
    echo "  WARN $path exists but is empty — $hint" >&2
    return 0
  fi
  mkdir -p "$(dirname "$path")"
  : >"$path"
  chmod 600 "$path" 2>/dev/null || true
  echo "  created placeholder $path — $hint" >&2
}

harden_secrets_dir() {
  mkdir -p "$SECRETS_DIR" "$ENCRYPTED_DIR"
  chmod 700 "$SECRETS_DIR" "$ENCRYPTED_DIR" 2>/dev/null || true
  echo "  secrets/ and secrets/encrypted/ (mode 700, ready for age|sops)"
}

install_encryption_templates() {
  if [[ ! -f .sops.yaml ]] && [[ -f .sops.yaml.example ]]; then
    echo "  Tip: cp .sops.yaml.example .sops.yaml and add your KMS/PGP/age rules."
  fi
  if [[ ! -f "$SECRETS_DIR/age-recipients.txt" ]] && [[ -f "$SECRETS_DIR/age-recipients.txt.example" ]]; then
    echo "  Tip: cp secrets/age-recipients.txt.example secrets/age-recipients.txt"
  fi
}

have_tool() {
  command -v "$1" >/dev/null 2>&1
}

read_secrets_encryption() {
  local v="${SECRETS_ENCRYPTION:-}"
  v="$(echo "$v" | tr '[:upper:]' '[:lower:]' | tr -d '\r')"
  if [[ -n "$v" ]]; then
    echo "${v:-none}"
    return
  fi
  v="$(grep -E '^SECRETS_ENCRYPTION=' "$ENV_TXT" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' | tr '[:upper:]' '[:lower:]' || true)"
  echo "${v:-none}"
}

# Basename without .txt for encrypted artifact names (private_key.txt → private_key.enc).
secret_base_name() {
  local name="$1"
  echo "${name%.txt}"
}

encrypt_one_age() {
  local name="$1"
  local src="$SECRETS_DIR/$name"
  local base
  base="$(secret_base_name "$name")"
  local dst="$ENCRYPTED_DIR/${base}.enc"
  local recipients="$SECRETS_DIR/age-recipients.txt"
  if [[ ! -s "$src" ]]; then
    return 0
  fi
  if [[ ! -f "$recipients" ]]; then
    echo "  SKIP age encrypt $name — missing $recipients (cp secrets/age-recipients.txt.example)" >&2
    return 0
  fi
  if ! have_tool age; then
    echo "  ERROR: age not installed (brew install age / apt install age)" >&2
    return 1
  fi
  age -R "$recipients" -o "$dst" "$src"
  chmod 600 "$dst" 2>/dev/null || true
  echo "  age -R … -o $dst ← $src"
}

encrypt_one_sops() {
  local name="$1"
  local src="$SECRETS_DIR/$name"
  local dst="$ENCRYPTED_DIR/$name.enc.yaml"
  if [[ ! -s "$src" ]]; then
    return 0
  fi
  if [[ ! -f .sops.yaml ]]; then
    echo "  SKIP sops encrypt $name — missing .sops.yaml" >&2
    return 0
  fi
  sops --encrypt --input-type binary --output-type binary "$src" >"$dst"
  chmod 600 "$dst" 2>/dev/null || true
  echo "  encrypted $src → $dst (sops)"
}

encrypt_secrets_at_rest() {
  local mode="$1"
  mkdir -p "$ENCRYPTED_DIR"
  case "$mode" in
    age)
      echo "  Using age → secrets/encrypted/*.enc (recipients: secrets/age-recipients.txt)"
      for name in "${SECRET_FILES[@]}"; do
        encrypt_one_age "$name"
      done
      ;;
    sops)
      if ! have_tool sops; then
        echo "  ERROR: sops not installed — https://github.com/getsops/sops" >&2
        return 1
      fi
      echo "  Using sops → secrets/encrypted/*.enc.yaml (.sops.yaml required)"
      for name in "${SECRET_FILES[@]}"; do
        encrypt_one_sops "$name"
      done
      ;;
    none)
      echo "  SECRETS_ENCRYPTION=none — plaintext only under secrets/ (chmod 600)."
      ;;
    *)
      echo "  WARN: unknown SECRETS_ENCRYPTION=$mode (use none|age|sops)" >&2
      return 1
      ;;
  esac
}

echo "=== Solana arb bot — secret layout (prod) ==="
echo "Repo: $ROOT"
echo "Env file: $ENV_TXT"
echo ""

bash "$ROOT/scripts/init-secrets-templates.sh" 2>/dev/null || true

harden_secrets_dir
install_encryption_templates

echo ""
echo "=== Ledger only in prod (no hot key in production) ==="
append_kv_if_missing "APP_ENV=production"
append_kv_if_missing "SIGNER_TYPE=ledger"
append_kv_if_missing "SIMULATE=false"
append_kv_if_missing "ALLOW_HOT_KEY_IN_PROD=0"
append_kv_if_missing "MIN_PROFIT_USDC=15.0"
append_kv_if_missing "MAX_FLASH_USDC=10000"
append_kv_if_missing "MAX_DAILY_LOSS_USDC=50"
append_kv_if_missing "MAX_SLIPPAGE_BPS=40"
append_kv_if_missing "KILL_SWITCH_ON_LOSS=1"
append_kv_if_missing "MEV_PROTECTION_ENABLED=true"
append_kv_if_missing "ML_ENABLED=1"
append_kv_if_missing "DYNAMIC_AMOUNT=true"
append_kv_if_missing "ALCHEMY_TRANSACT_ENABLED=true"
append_kv_if_missing "ALERT_ENABLED=1"
append_kv_if_missing "PAGERDUTY_ENABLED=1"
append_kv_if_missing "HARDWARE_ADDRESS="
append_kv_if_missing "FLASH_LOAN_CONTRACT="
append_kv_if_missing "LEDGER_DEPLOYER_ADDRESS="
append_kv_if_missing "GNOSIS_SAFE_ADDRESS="
append_kv_if_missing "TIMELOCK_ADDRESS="
append_kv_if_missing "DENY_DOT_ENV_TXT=true"
append_kv_if_missing "SECRET_MANAGER=local"
append_kv_if_missing "SECRETS_ENCRYPTION=none"
append_kv_if_missing "TEST_MODE=false"
append_kv_if_missing "LIVE_TRADING_CONFIRM="
append_kv_if_missing "CEX_DEX_MIN_NET_SPREAD_BPS=45"
append_kv_if_missing "CEX_DEX_MAX_TRADE_USDC_MICRO=50000000"
append_kv_if_missing "CEX_DEX_FLASH_SIM_DEBUG=false"
append_kv_if_missing "LIVE_TRADE_COOLDOWN_SECONDS=120"
append_kv_if_missing "MAX_LIVE_TRADES_PER_HOUR=6"

echo ""
echo "=== secrets/ files for docker-compose.prod.yml ==="

for name in "${SECRET_FILES[@]}"; do
  ensure_secret_file "$SECRETS_DIR/$name" "Populate manually; never commit plaintext."
done

for f in "${SECRET_FILES[@]}"; do
  path="$SECRETS_DIR/$f"
  if [[ -f "$path" ]]; then
    chmod 600 "$path" 2>/dev/null || true
  fi
done

ENC_MODE="$(read_secrets_encryption)"
echo ""
echo "=== Encryption at rest ($ENC_MODE) ==="
if [[ "${ENCRYPT_SECRETS:-0}" == "1" ]] && [[ "$ENC_MODE" == "age" || "$ENC_MODE" == "sops" ]]; then
  encrypt_secrets_at_rest "$ENC_MODE"
elif [[ "$ENC_MODE" == "age" || "$ENC_MODE" == "sops" ]]; then
  echo "  SECRETS_ENCRYPTION=$ENC_MODE — run: ENCRYPT_SECRETS=1 bash scripts/setup-secrets.sh"
  echo "  or: bash scripts/encrypt-secrets.sh"
else
  echo "  Set SECRETS_ENCRYPTION=age|sops in $ENV_TXT (or export SECRETS_ENCRYPTION=age), then encrypt."
fi

echo ""
echo "=== .env (non-secret runtime) ==="
if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    echo "  Copied .env.example → .env (review before prod deploy)."
  else
    touch .env
    echo "  Created empty .env"
  fi
else
  echo "  .env already present"
fi

echo ""
echo "Done."
echo "  1. Paste real values into secrets/* (chmod 600). Never commit plaintext secrets/."
echo "  2. Optional: SECRETS_ENCRYPTION=age|sops → bash scripts/encrypt-secrets.sh"
echo "  3. Before compose: bash scripts/decrypt-secrets.sh (if using encrypted/)"
echo "  4. Keep PRIVATE_KEY empty in .env when using Docker *_FILE mounts."
echo "  5. Prod: SIGNER_TYPE=ledger only — run monitor on air-gapped host (see docs/SIGNING.md)."
echo "  6. Deploy: npm run sync:compose-env && docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.prod.override.yml up --build -d"
echo "  Rotate test keys only: bash scripts/rotate_secrets.sh"
