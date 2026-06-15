"""Best bid aggregation for v2 reverse detection (execution stays on Backpack)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_DISCOVERY_FACTORIES: dict[str, Any] = {}


def _discovery_factories() -> dict[str, Any]:
    global _DISCOVERY_FACTORIES
    if not _DISCOVERY_FACTORIES:
        from src.cex.ccxt_wrapper import (
            create_backpack_exchange,
            create_bybit_exchange,
            create_kucoin_exchange,
            create_okx_exchange,
        )

        _DISCOVERY_FACTORIES = {
            "backpack": create_backpack_exchange,
            "bybit": create_bybit_exchange,
            "okx": create_okx_exchange,
            "kucoin": create_kucoin_exchange,
        }
    return _DISCOVERY_FACTORIES


def parse_cex_venues() -> list[str]:
    """Comma-separated venues for bid discovery (sell leg remains Backpack)."""
    raw = (
        os.getenv("V2_CEX_VENUES")
        or os.getenv("CEX_PRICE_FEED_VENUES")
        or "backpack,bybit,okx,kucoin"
    )
    return [v.strip().lower() for v in raw.split(",") if v.strip()]


async def _fetch_ccxt_bid(venue: str, symbol: str) -> float | None:
    factories = _discovery_factories()
    factory = factories.get(venue)
    if factory is None:
        return None
    for sym in (f"{symbol}/USDC", f"{symbol}/USDT"):
        try:
            ticker = await asyncio.to_thread(factory().fetch_ticker, sym)
        except Exception as exc:
            logger.debug("CEX %s %s bid: %s", venue, sym, exc)
            continue
        bid = ticker.get("bid")
        if bid and float(bid) > 0:
            return float(bid)
    return None


class CexBidAggregator:
    """Pick the highest bid across configured venues for spread detection."""

    def __init__(self, backpack: Any) -> None:
        self.backpack = backpack
        self.venues = parse_cex_venues()
        self.last_best_venue: str = ""

    async def best_bid(self, symbol: str = "SOL") -> tuple[float | None, str]:
        quotes: dict[str, float] = {}

        if "backpack" in self.venues:
            try:
                bp = await self.backpack.get_bid_price(symbol)
            except Exception as exc:
                logger.debug("Backpack bid failed: %s", exc)
                bp = None
            if bp and float(bp) > 0:
                quotes["backpack"] = float(bp)

        for venue in self.venues:
            if venue == "backpack":
                continue
            bid = await _fetch_ccxt_bid(venue, symbol)
            if bid and bid > 0:
                quotes[venue] = bid

        if not quotes:
            self.last_best_venue = ""
            return None, ""

        best_venue = max(quotes, key=quotes.get)
        best = quotes[best_venue]
        self.last_best_venue = best_venue
        if len(quotes) > 1:
            logger.debug(
                "CEX_BID_AGG | best=%.4f venue=%s quotes=%s",
                best,
                best_venue,
                {k: round(v, 4) for k, v in quotes.items()},
            )
        return best, best_venue
