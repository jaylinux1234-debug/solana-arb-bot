# price_health.py
import asyncio
import logging

from src.cex.price_feed import cex_feed

logger = logging.getLogger(__name__)


async def price_health_monitor():
    """Background task to monitor SOL price feed health"""
    while True:
        price = await cex_feed.get_price("SOL/USDC")
        if not price:
            logger.warning("🚨 SOL/USDC price feed is DOWN! Check CEX connections.")
        else:
            logger.debug(f"Price health OK: SOL/USDC = ${price:.2f}")
        await asyncio.sleep(60)  # check every minute
