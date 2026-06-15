"""Single JSONL attempt log for v2 (no near-miss file)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def get_last_collateral_fill(path: str | None = None) -> str | None:
    """Return UTC timestamp of the most recent successful collateral fill."""
    log_path = Path(path or os.getenv("V2_ATTEMPTS_LOG", "logs/v2_attempts.jsonl"))
    if not log_path.is_file():
        return None
    last_ts: str | None = None
    try:
        with log_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                lane = str(row.get("lane") or row.get("strategy") or "").lower()
                if lane != "collateral_swap":
                    continue
                if not (row.get("live_fill") or row.get("executed")):
                    continue
                ts = str(row.get("ts") or "")
                if ts:
                    last_ts = ts
    except OSError as exc:
        logger.debug("collateral fill scan failed: %s", exc)
    if not last_ts:
        return None
    try:
        dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M UTC")
    except ValueError:
        return last_ts


def append_attempt(path: str, record: dict[str, Any]) -> None:
    row = {
        **record,
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "v2_attempt",
    }
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError as exc:
        logger.warning("v2 attempt log write failed: %s", exc)
