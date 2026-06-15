"""Strategy loop: CEX-DEX cycle with graceful shutdown and resource cleanup."""

from __future__ import annotations

import asyncio
import logging
import signal

from src.config.settings import Settings
from src.execution.jito import close_jito_aiohttp_session
from src.monitoring.health_server import set_redis_status
from src.strategies.cex_dex_cycle import CexDexCycle
from src.utils.redis import close_redis, ping_redis, redis_client

logger = logging.getLogger(__name__)


class StrategyOrchestrator:
    """Runs the primary CEX-DEX cycle until shutdown."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cycle = CexDexCycle.create()
        self._shutdown = asyncio.Event()

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        def request_shutdown() -> None:
            if self._shutdown.is_set():
                return
            logger.info("Shutting down...")
            self._shutdown.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, request_shutdown)
            except NotImplementedError:
                if sig == signal.SIGINT:
                    logger.debug("SIGINT handler not supported on this platform")
                break

    async def _prime_redis(self) -> None:
        r = await redis_client()
        if r is None:
            set_redis_status(None)
            return
        ok = await ping_redis()
        set_redis_status(ok)
        logger.info("Redis %s", "ok" if ok else "unavailable")

    async def run_forever(self) -> None:
        await self._prime_redis()

        loop = asyncio.get_running_loop()
        self._install_signal_handlers(loop)

        cycle_task = asyncio.create_task(self._cycle.run_forever(), name="cex_dex_cycle")

        def _on_cycle_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error("CEX-DEX cycle exited with error", exc_info=exc)
            else:
                logger.warning("CEX-DEX cycle exited cleanly")
            self._shutdown.set()

        cycle_task.add_done_callback(_on_cycle_done)

        try:
            await self._shutdown.wait()
        except KeyboardInterrupt:
            self._shutdown.set()
        finally:
            if not cycle_task.done():
                cycle_task.cancel()
            await asyncio.gather(cycle_task, return_exceptions=True)
            await close_jito_aiohttp_session()
            await close_redis()
            logger.info("Strategy orchestrator stopped")
