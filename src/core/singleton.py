"""Process singleton lock (next-level Redis guard or legacy Redis/port)."""

from __future__ import annotations

import asyncio
import logging
import os

from src.config.settings import Settings
from src.core.security import acquire_bot_singleton_lock, release_bot_singleton_lock
from src.core.singleton_guard import (
    acquire_nextlevel_singleton,
    redis_url,
    release_nextlevel_singleton,
)

logger = logging.getLogger(__name__)
_acquired = False
_nextlevel = False


def _use_nextlevel_guard() -> bool:
    raw = os.getenv("BOT_SINGLETON_NEXTLEVEL_GUARD")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(redis_url())


async def ensure_singleton(settings: Settings) -> None:
    """Acquire distributed singleton before trading loop starts."""
    global _acquired, _nextlevel
    _nextlevel = _use_nextlevel_guard()
    if _nextlevel:
        await asyncio.to_thread(acquire_nextlevel_singleton, log=logger)
    else:
        await asyncio.to_thread(acquire_bot_singleton_lock, logger=logger)
    _acquired = True


def release_singleton() -> None:
    """Release lock acquired by :func:`ensure_singleton`."""
    global _acquired, _nextlevel
    if not _acquired:
        return
    if _nextlevel:
        release_nextlevel_singleton()
    else:
        release_bot_singleton_lock()
    _acquired = False
    _nextlevel = False
