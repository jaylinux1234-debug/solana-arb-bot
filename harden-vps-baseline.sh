#!/usr/bin/env bash
set -euo pipefail

# Compatibility wrapper so existing runbooks can call this from repo root.
exec bash ops/harden-vps-baseline.sh
