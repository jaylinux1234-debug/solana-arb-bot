#!/usr/bin/env bash
set -euo pipefail

# Baseline hardening helper for Ubuntu/Debian VPS.
# Run as root on the VPS. Review before use.

SSH_PORT="${SSH_PORT:-22}"
ALLOW_WEBHOOK="${ALLOW_WEBHOOK:-false}"
WEBHOOK_PORT="${WEBHOOK_PORT:-8799}"

apt-get update
apt-get install -y ufw fail2ban

ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}"/tcp

if [[ "${ALLOW_WEBHOOK}" == "true" ]]; then
  ufw allow "${WEBHOOK_PORT}"/tcp
fi

ufw --force enable
systemctl enable --now fail2ban

echo "[ok] UFW + fail2ban enabled"
ufw status verbose
fail2ban-client status || true
