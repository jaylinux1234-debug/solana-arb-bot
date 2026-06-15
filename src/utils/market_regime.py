# src/utils/market_regime.py
"""Volatility-based market regime detection for ML retraining."""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


async def get_volatility_24h() -> float:
    """Best-effort 24h volatility proxy (CEX bid–ask width in bps)."""
    try:
        from src.cex.price_feed import cex_feed

        _, vol = await cex_feed.get_price_and_volatility_bps("SOL/USDC")
        return float(vol or 0.0)
    except Exception as exc:
        logger.debug("get_volatility_24h failed: %s", exc)
        return float(os.getenv("DEFAULT_VOLATILITY_BPS", "80"))


async def detect_market_regime_async() -> int:
    """
    Regime codes: 0=trending, 1=ranging, 2=volatile.
    """
    vol = await get_volatility_24h()
    return _regime_from_volatility(vol)


def detect_market_regime() -> int:
    """
    Sync regime detection (uses live feed when no event loop is running).
    """
    try:
        asyncio.get_running_loop()
        vol = float(os.getenv("DEFAULT_VOLATILITY_BPS", "80"))
        logger.debug("detect_market_regime: using DEFAULT_VOLATILITY_BPS=%s (loop active)", vol)
    except RuntimeError:
        vol = asyncio.run(get_volatility_24h())
    return _regime_from_volatility(vol)


def _regime_from_volatility(vol: float) -> int:
    if vol > 180:
        return 2
    if vol > 90:
        return 0
    return 1
