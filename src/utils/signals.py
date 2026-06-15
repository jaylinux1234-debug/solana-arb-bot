"""Signal lifecycle helpers — prevents backrun wipeout across MEV poll cycles."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

_backrun_cache: dict[str, Any] = {}
_ttl_tasks: dict[str, asyncio.Task[None]] = {}
_BACKRUN_KEY = "backrun_active"


def _default_ttl_sec() -> float:
    try:
        return float(os.getenv("BACKRUN_SIGNAL_TTL_SEC", "30"))
    except (TypeError, ValueError):
        return 30.0


def _is_expired(ctx: dict[str, Any]) -> bool:
    ttl_until = ctx.get("ttl_until")
    if ttl_until is None:
        return False
    try:
        return time.monotonic() >= float(ttl_until)
    except (TypeError, ValueError):
        return True


def set_backrun_ttl(ctx: dict[str, Any], ttl_sec: float | None = None) -> None:
    """Store backrun context with auto-expiry (async task + monotonic fallback)."""
    ttl = float(ttl_sec if ttl_sec is not None else _default_ttl_sec())
    stored = {**ctx, "ttl_until": time.monotonic() + ttl}
    _backrun_cache[_BACKRUN_KEY] = stored

    existing = _ttl_tasks.get(_BACKRUN_KEY)
    if existing is not None and not existing.done():
        existing.cancel()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    _ttl_tasks[_BACKRUN_KEY] = loop.create_task(_expire_backrun(_BACKRUN_KEY, ttl))


async def _expire_backrun(key: str, ttl_sec: float) -> None:
    try:
        await asyncio.sleep(ttl_sec)
    except asyncio.CancelledError:
        return
    _backrun_cache.pop(key, None)
    _ttl_tasks.pop(key, None)
    _sync_brain_backrun_inactive()


def _sync_brain_backrun_inactive() -> None:
    try:
        from src.strategies.brain_signals import note_backrun_context

        note_backrun_context({"active": False, "amount_micro": 0}, skip_ttl=True)
    except Exception:
        pass


def get_backrun_context() -> dict[str, Any]:
    ctx = _backrun_cache.get(_BACKRUN_KEY)
    if not isinstance(ctx, dict):
        return {"active": False}
    if _is_expired(ctx):
        _backrun_cache.pop(_BACKRUN_KEY, None)
        return {"active": False}
    return dict(ctx)


def reset_cycle_signals() -> None:
    """Only reset non-persistent signals; backrun TTL handles expiry."""
    return
