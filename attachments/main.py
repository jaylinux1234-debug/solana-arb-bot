#!/usr/bin/env python3
"""
Main Entry Point - High Win-Rate Solana CEX-DEX Arbitrage Bot
Production Ready with Paid RPC + Helius + Dynamic Sizing
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime

from src.config.settings import bootstrap_config, get_settings
from src.strategies.cex_dex_core import CexDexCore
from src.monitoring.health import start_health_server
from src.core.wallet import initialize_wallet
from src.monitoring.metrics import init_metrics

logger = logging.getLogger(__name__)
settings = get_settings()

# Global strategy instance
strategy: CexDexCore = None


async def graceful_shutdown():
    """Clean shutdown handler"""
    logger.info("🛑 Shutting down bot gracefully...")
    if strategy:
        await strategy.close()
    logger.info("✅ Bot shutdown complete.")
    sys.exit(0)


async def main_loop():
    """Main trading loop"""
    global strategy

    logger.info(f"🚀 Starting Solana CEX-DEX Arb Bot | Env: {settings.APP_ENV}")
    logger.info(f"AI Confidence Floor: {settings.CEX_DEX_AI_CONFIDENCE_FLOOR}% | "
                f"Min Net Spread: {settings.CEX_DEX_MIN_NET_SPREAD_BPS}bps")

    # Initialize components
    await initialize_wallet()
    strategy = CexDexCore()

    # Start health check server
    if settings.ENABLE_BOT_HEALTH_SERVER:
        await start_health_server()

    init_metrics()

    consecutive_errors = 0
    max_errors = 8

    while True:
        try:
            # Scan for opportunity
            opportunity = await strategy.scan()

            if opportunity:
                logger.info(f"💎 Opportunity detected | Net: {opportunity.net_bps:.1f}bps | "
                          f"AI: {opportunity.confidence}% | Size: ${opportunity.size_usdc_micro/1_000_000:.1f}k")

                success = await strategy.execute(opportunity)

                if success:
                    logger.info("🎉 Trade completed successfully!")
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
            else:
                # No opportunity — quiet log
                if datetime.now().second % 30 == 0:  # log every 30s
                    logger.debug("No strong opportunity in this cycle")

            # Cooldown between cycles
            await asyncio.sleep(settings.CEX_DEX_ORACLE_POLL_MIN_SEC)

        except asyncio.CancelledError:
            break
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Cycle error: {e}", exc_info=True)

            if consecutive_errors >= max_errors:
                logger.critical("Too many consecutive errors. Shutting down.")
                break

            await asyncio.sleep(8)


async def run():
    """Entry point"""
    # Bootstrap configuration
    bootstrap_config()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("logs/bot.log", mode='a')
        ]
    )

    # Register shutdown handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(graceful_shutdown()))

    try:
        await main_loop()
    except KeyboardInterrupt:
        await graceful_shutdown()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
    finally:
        await graceful_shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user.")