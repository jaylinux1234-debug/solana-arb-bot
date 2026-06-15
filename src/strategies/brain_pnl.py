"""
Rolling realized PnL (USD) for dynamic AI minimum confidence.

Append samples via ``append_realized_pnl_usd`` (called from ``circuit_breaker.record_trade``).
``cex_dex_ai_min_confidence`` applies an extra floor when recent PnL is negative.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _state_path() -> Path:
    return Path(os.getenv("PNL_CONFIDENCE_STATE_PATH", "logs/pnl_confidence_window.json"))


def append_realized_pnl_usd(pnl_usd: float) -> None:
    """Record one closed-trade PnL sample (USD, negative = loss)."""
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                rows = [x for x in raw if isinstance(x, dict)]
        except Exception as exc:
            logger.debug("pnl_confidence: reset window (%s)", exc)
            rows = []
    rows.append({"t": time.time(), "pnl": float(pnl_usd)})
    max_rows = max(10, int(os.getenv("PNL_CONFIDENCE_MAX_SAMPLES", "500")))
    rows = rows[-max_rows:]
    p.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    try:
        from src.monitoring.metrics import refresh_realized_profit_gauge

        refresh_realized_profit_gauge()
    except Exception as exc:
        logger.debug("pnl gauge refresh skipped: %s", exc)


def realized_pnl_sum_all() -> float:
    """Sum of all stored realized PnL samples (USD)."""
    p = _state_path()
    if not p.is_file():
        return 0.0
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return 0.0
    except Exception:
        return 0.0
    total = 0.0
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            total += float(row.get("pnl", 0.0))
        except (TypeError, ValueError):
            continue
    return total


def rolling_pnl_sum_usd(*, window_seconds: float) -> float:
    """Sum of ``pnl`` for samples newer than ``window_seconds`` ago."""
    p = _state_path()
    if not p.is_file():
        return 0.0
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return 0.0
    except Exception:
        return 0.0
    cutoff = time.time() - window_seconds
    total = 0.0
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            t = float(row.get("t", 0))
            if t >= cutoff:
                total += float(row.get("pnl", 0.0))
        except (TypeError, ValueError):
            continue
    return total


def bump_min_confidence_for_recent_pnl(base: int) -> int:
    """
    Raise ``base`` when rolling-window PnL is negative (more selective AI approvals).
    """
    hours = float(os.getenv("AI_PNL_CONFIDENCE_WINDOW_HOURS", "72"))
    window_sec = max(3600.0, hours * 3600.0)
    usd = rolling_pnl_sum_usd(window_seconds=window_sec)
    neutral = float(os.getenv("AI_PNL_CONFIDENCE_NEUTRAL_USD", "0"))
    if usd >= neutral:
        return base
    loss = neutral - usd
    per = float(os.getenv("AI_PNL_CONFIDENCE_BUMP_PER_LOSS_USD", "40"))
    max_bump = int(os.getenv("AI_PNL_CONFIDENCE_MAX_BUMP", "20"))
    if per <= 0:
        return base
    bump = min(max_bump, int(loss / per))
    return min(95, base + bump)
