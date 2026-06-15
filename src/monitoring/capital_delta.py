"""On-chain USDC snapshots before/after fills for Phase 1 capital tracking."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_last_usdc: float | None = None


def _delta_log_path() -> Path:
    return Path(os.getenv("CAPITAL_DELTA_LOG", "logs/capital_delta.jsonl"))


def _enabled() -> bool:
    raw = (os.getenv("ENABLE_CAPITAL_DELTA_LOG", "true") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


async def fetch_onchain_usdc() -> float:
    from src.utils.inventory import get_usdc_balance_async

    return float(await get_usdc_balance_async())


async def log_capital_delta(
    action: str,
    *,
    strategy: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a capital snapshot; returns row including delta vs prior reading."""
    global _last_usdc

    if not _enabled():
        return {}

    try:
        usdc = await fetch_onchain_usdc()
    except Exception as exc:
        logger.debug("capital_delta balance read failed: %s", exc)
        return {}

    delta = None if _last_usdc is None else round(usdc - _last_usdc, 4)
    row: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "action": action,
        "strategy": strategy or None,
        "onchain_usdc": round(usdc, 4),
        "delta_usdc": delta,
        "note": "Phase 1 tracking",
    }
    if extra:
        row.update(extra)

    log_path = _delta_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError as exc:
        logger.warning("capital_delta log write failed: %s", exc)

    logger.info(
        "Capital delta | $%.4f delta=%s action=%s strategy=%s",
        usdc,
        f"{delta:+.4f}" if delta is not None else "n/a",
        action,
        strategy or "-",
    )

    try:
        from src.monitoring.metrics import set_onchain_usdc_balance

        set_onchain_usdc_balance(usdc)
    except Exception:
        pass

    _last_usdc = usdc
    return row


async def log_capital_delta_before_after(
    action: str,
    *,
    strategy: str = "",
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convenience: log pre-action then return callable for post-action log."""
    before = await log_capital_delta(f"{action}_before", strategy=strategy, extra=extra)
    return before, before  # caller logs after separately


def log_capital_delta_sync(action: str, *, usdc: float | None = None) -> dict[str, Any]:
    """Sync CLI helper when asyncio loop is awkward."""
    import asyncio

    if usdc is not None:
        global _last_usdc
        row = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action,
            "onchain_usdc": round(usdc, 4),
            "note": "Phase 1 tracking (manual)",
        }
        log_path = _delta_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        _last_usdc = usdc
        return row
    return asyncio.run(log_capital_delta(action))
