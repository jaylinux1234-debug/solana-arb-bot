"""
Unified strategy router facade.

``StrategyRouter`` (in ``router.py``) owns the hybrid CEX-DEX + MEV loop.
MEV lane execution is centralized in ``mev_dispatch.execute_mev_lane`` and
re-exported here as ``dispatch_mev_lane`` for callers that import this module.
"""

from __future__ import annotations

from typing import Any

from src.config.settings import Settings, get_settings
from src.strategies.mev_dispatch import execute_mev_lane as dispatch_mev_lane
from src.strategies.router import (
    StrategyRouter,
    get_active_router,
    mev_status_snapshot,
)

__all__ = [
    "StrategyRouter",
    "create_strategy_router",
    "dispatch_mev_lane",
    "execute_mev_lane",
    "get_active_router",
    "mev_status_snapshot",
    "refresh_mev_brain_snapshots",
]

# Back-compat alias
execute_mev_lane = dispatch_mev_lane


def create_strategy_router(
    risk_engine: Any,
    inventory: Any | None = None,
    *,
    settings: Settings | None = None,
    shutdown_event: Any | None = None,
    mev_only: bool | None = None,
) -> StrategyRouter:
    """Factory for v2 hybrid boot and tests."""
    return StrategyRouter(
        risk_engine,
        inventory,
        settings=settings or get_settings(),
        shutdown_event=shutdown_event,
        mev_only=mev_only,
    )


async def refresh_mev_brain_snapshots(strategy: Any | None = None) -> None:
    """Refresh collateral + liquidation brain signals (backrun TTL preserved)."""
    from src.strategies.multi_strategy_cycle import (
        refresh_collateral_brain_snapshot,
        refresh_liquidation_brain_snapshot,
    )

    if strategy is not None:
        await refresh_collateral_brain_snapshot(strategy)
    await refresh_liquidation_brain_snapshot()
