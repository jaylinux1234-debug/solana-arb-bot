"""Start all enabled extended meme lanes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.strategies.filter_discovery import discovery_loop
from src.strategies.hybrid_mev_meme import hybrid_mev_watch_loop
from src.strategies.meme_lanes_config import (
    get_filter_discovery_settings,
    get_hybrid_mev_settings,
    get_migration_sniper_settings,
    get_smart_money_settings,
)
from src.strategies.migration_sniper import migration_sniper_loop
from src.strategies.position_manager import position_manager
from src.strategies.smart_money_copy import smart_money_copy_loop

logger = logging.getLogger(__name__)


async def run_meme_lanes(shutdown_event: asyncio.Event | None = None) -> None:
    """Launch smart money, migration, filter discovery, and hybrid MEV loops."""
    tasks: list[asyncio.Task[Any]] = []
    names: list[str] = []

    sm = get_smart_money_settings()
    if sm.enabled:
        tasks.append(asyncio.create_task(smart_money_copy_loop(shutdown_event), name="smart_money_copy"))
        names.append("smart_money_copy")

    mig = get_migration_sniper_settings()
    if mig.enabled:
        tasks.append(asyncio.create_task(migration_sniper_loop(shutdown_event), name="migration_sniper"))
        names.append("migration_sniper")

    filt = get_filter_discovery_settings()
    if filt.enabled:
        tasks.append(asyncio.create_task(discovery_loop(shutdown_event), name="filter_discovery"))
        names.append("filter_discovery")

    hybrid = get_hybrid_mev_settings()
    if hybrid.enabled:
        tasks.append(asyncio.create_task(hybrid_mev_watch_loop(shutdown_event), name="hybrid_mev_meme"))
        names.append("hybrid_mev_meme")

    if not tasks:
        logger.info("meme_lanes: no extended lanes enabled")
        return

    logger.info("meme_lanes started | lanes=%s", names)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for name, result in zip(names, results, strict=True):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            logger.error("meme_lane %s failed: %s", name, result, exc_info=result)


def get_meme_lanes_stats() -> dict[str, Any]:
    return {"position_manager": position_manager.stats()}
