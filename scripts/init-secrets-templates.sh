#!/usr/bin/env bash
# Create secrets/.local templates + secrets/encrypted (chmod 600). Ledger prod: leave private_key*.txt empty.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
SECRETS_DIR="$ROOT/secrets/.local"
ENCRYPTED_DIR="$ROOT/secrets/encrypted"

mkdir -p "$SECRETS_DIR" "$ENCRYPTED_DIR"
chmod 700 "$ROOT/secrets" "$SECRETS_DIR" "$ENCRYPTED_DIR" 2>/dev/null || true

write_template() {
  local path="$1"
  local body="$2"
  if [[ -s "$path" ]]; then
    echo "  skip $path (already has content)"
    return 0
  fi
  printf '%s\n' "$body" >"$path"
  chmod 600 "$path" 2>/dev/null || true
  echo "  created $path"
}

write_template "$SECRETS_DIR/private_key.txt" "$(cat <<'EOF'
# ONLY if NOT using Ledger. Otherwise leave empty or use hardware.
your_private_key_here_without_0x_if_needed
EOF
)"

write_template "$SECRETS_DIR/oneinch_api_key.txt" "$(cat <<'EOF'
your_1inch_api_key
EOF
)"

write_template "$SECRETS_DIR/cow_api_key.txt" "$(cat <<'EOF'
your_cow_api_key
EOF
)"

write_template "$SECRETS_DIR/pagerduty_routing_key.txt" "$(cat <<'EOF'
your_pagerduty_integration_key
EOF
)"

for name in private_key private_key_cex_dex jupiter_api_key openai_api_key backpack_secret helius_api_key; do
  write_template "$SECRETS_DIR/$name" "# Optional — paste value or leave empty"
done

chmod 600 "$SECRETS_DIR"/* 2>/dev/null || true

echo ""
echo "Done. Edit secrets/.local/* then: bash scripts/encrypt-secrets.sh"
echo "  Prod + Ledger: keep private_key.txt empty; use encrypted/*.enc.yaml in Docker."
