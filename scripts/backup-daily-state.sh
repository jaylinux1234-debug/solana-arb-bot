#!/usr/bin/env bash
# Daily Redis + wallet safety snapshot — cron: 0 3 * * * cd /path/to/repo && bash scripts/backup-daily-state.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
python scripts/backup-daily-state.py
