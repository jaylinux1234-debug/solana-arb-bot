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
_backpack_usdc: float | None = None
_backpack_sol: float | None = None
_backpack_balances_at: float = 0.0


def record_backpack_balance_check(ok: bool) -> None:
    global _backpack_balance_ok, _backpack_balance_checked_at
    _backpack_balance_ok = ok
    _backpack_balance_checked_at = time.time()


def record_backpack_balances(usdc: float, sol: float | None = None) -> None:
    """Thread-safe snapshot for health probes (main loop updates this)."""
    global _backpack_usdc, _backpack_sol, _backpack_balances_at
    _backpack_usdc = float(usdc)
    if sol is not None:
        _backpack_sol = float(sol)
    _backpack_balances_at = time.time()


def get_cached_backpack_usdc(*, max_age_sec: float = 180.0) -> float | None:
    if _backpack_usdc is None or _backpack_balances_at <= 0:
        return None
    if time.time() - _backpack_balances_at > max_age_sec:
        return None
    return float(_backpack_usdc)


def get_cached_backpack_sol(*, max_age_sec: float = 180.0) -> float | None:
    if _backpack_sol is None or _backpack_balances_at <= 0:
        return None
    if time.time() - _backpack_balances_at > max_age_sec:
        return None
    return float(_backpack_sol)


def _parse_usdc_from_balances(balance: Any) -> float | None:
    if isinstance(balance, list):
        for item in balance:
            if str(item.get("asset", "")).upper() == "USDC":
                return float(item.get("available", 0) or 0)
    if isinstance(balance, dict):
        entry = balance.get("USDC") or balance.get("usdc")
        if isinstance(entry, dict):
            return float(entry.get("available", 0) or 0)
        if entry is not None:
            return float(entry)
        for key in ("balances", "data", "result"):
            nested = balance.get(key)
            if isinstance(nested, list):
                parsed = _parse_usdc_from_balances(nested)
                if parsed is not None:
                    return parsed
    return None


def get_backpack_balance_status() -> dict[str, Any]:
    status: dict[str, Any] = {
        "ok": _backpack_balance_ok,
        "checked_at": _backpack_balance_checked_at or None,
        "age_seconds": round(time.time() - _backpack_balance_checked_at, 1)
        if _backpack_balance_checked_at
        else None,
    }
    if _backpack_usdc is not None:
        status["usdc"] = round(_backpack_usdc, 2)
    if _backpack_sol is not None:
        status["sol"] = round(_backpack_sol, 6)
    if _backpack_balances_at:
        status["balances_age_seconds"] = round(time.time() - _backpack_balances_at, 1)
    return status


async def check_backpack_balance_async() -> bool:
    """Async balance probe — True when Backpack responds with balance data."""
    from src.cex.backpack import get_backpack_client

    client = get_backpack_client()
    balance = await client.get_balances()
    if not balance:
        logger.warning("Backpack balance fetch failed - possible auth issue")
        record_backpack_balance_check(False)
        return False

    usdc = _parse_usdc_from_balances(balance)
    if usdc is not None:
        record_backpack_balances(usdc)
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
