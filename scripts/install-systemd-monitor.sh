#!/bin/bash
# Install systemd unit for restart-monitor.sh (Linux prod host).
# Usage:
#   sudo bash scripts/install-systemd-monitor.sh
#   sudo bash scripts/install-systemd-monitor.sh --name flash-monitor
#   sudo bash scripts/install-systemd-monitor.sh --user deploy --no-start
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="solana-arb-monitor"
RUN_USER="root"
DO_START=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      SERVICE_NAME="${2:?}"
      shift 2
      ;;
    --user)
      RUN_USER="${2:?}"
      shift 2
      ;;
    --no-start)
      DO_START=0
      shift
      ;;
    -h|--help)
      echo "Usage: sudo $0 [--name flash-monitor] [--user root] [--no-start]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run as root (sudo)" >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "ERROR: systemd not found (Linux server only)" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not installed" >&2
  exit 1
fi

TEMPLATE="$ROOT/deploy/systemd/solana-arb-monitor.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
RESTART_SH="$ROOT/scripts/restart-monitor.sh"

if [[ ! -f "$RESTART_SH" ]]; then
  echo "ERROR: missing $RESTART_SH" >&2
  exit 1
fi

chmod +x "$RESTART_SH"

# Pick npm path for ExecStartPre
NPM_BIN="$(command -v npm || true)"
if [[ -z "$NPM_BIN" ]]; then
  NPM_BIN="/usr/bin/npm"
fi

sed \
  -e "s|/path/to/solana-arb-bot|$ROOT|g" \
  -e "s|^User=root|User=$RUN_USER|" \
  -e "s|ExecStartPre=.*|ExecStartPre=$NPM_BIN run sync:compose-env|" \
  -e "s|SOPS_AGE_KEY_FILE=/path/to/solana-arb-bot/secrets/.local/sops_age_key|SOPS_AGE_KEY_FILE=$ROOT/secrets/.local/sops_age_key|" \
  "$TEMPLATE" >"$UNIT_PATH"

chmod 644 "$UNIT_PATH"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

if [[ "$DO_START" -eq 1 ]]; then
  systemctl restart "${SERVICE_NAME}.service"
  systemctl --no-pager status "${SERVICE_NAME}.service" || true
else
  echo "Enabled ${SERVICE_NAME}.service (not started; use: systemctl start ${SERVICE_NAME})"
fi

echo ""
echo "Installed: $UNIT_PATH"
echo "  journal:  journalctl -u ${SERVICE_NAME} -f"
echo "  stop:     systemctl stop ${SERVICE_NAME}"
echo "  disable:  systemctl disable ${SERVICE_NAME}"
