"""CEX connectivity monitoring (Backpack auth / balance)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_backpack_balance_ok: bool | None = None
_backpack_balance_checked_at: float = 0.0


def record_backpack_balance_check(ok: bool) -> None:
    global _backpack_balance_ok, _backpack_balance_checked_at
    _backpack_balance_ok = ok
    _backpack_balance_checked_at = time.time()


def get_backpack_balance_status() -> dict[str, Any]:
    return {
        "ok": _backpack_balance_ok,
        "checked_at": _backpack_balance_checked_at or None,
        "age_seconds": round(time.time() - _backpack_balance_checked_at, 1)
        if _backpack_balance_checked_at
        else None,
    }


async def check_backpack_balance_async() -> bool:
    """Async balance probe — True when Backpack responds with balance data."""
    from src.cex.backpack import get_backpack_client

    client = get_backpack_client()
    balance = await client.get_balances()
    if not balance:
        logger.warning("Backpack balance fetch failed - possible auth issue")
        record_backpack_balance_check(False)
        return False

    record_backpack_balance_check(True)
    return True


def check_backpack_balance() -> bool:
    """Sync wrapper for background thread / health probes."""
    return asyncio.run(check_backpack_balance_async())


async def backpack_balance_monitor_loop(interval_sec: float | None = None) -> None:
    """Background task: periodic Backpack balance/auth check."""
    if interval_sec is None:
        try:
            interval_sec = float(os.getenv("BACKPACK_BALANCE_CHECK_INTERVAL_SEC", "60"))
        except (TypeError, ValueError):
            interval_sec = 60.0
    interval_sec = max(30.0, float(interval_sec))

    while True:
        try:
            await check_backpack_balance_async()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Backpack balance monitor: %s", exc)
            record_backpack_balance_check(False)
        await asyncio.sleep(interval_sec)
