#!/usr/bin/env python3
"""
Base chain monitor loop — multi-provider WebSocket with automatic failover.

Uses ``get_robust_ws_provider()`` from ``python.rpc_config`` (see ``src.core.eth_ws_provider``).
WS sessions are guarded by ``ws_breaker`` (pybreaker) with exponential backoff + Prometheus metrics.
"""

from __future__ import annotations

import asyncio
import logging
import os

import pybreaker
from dotenv import load_dotenv
from python.alerts import maybe_dispatch_rpc_failure_alert
from python.rpc_config import RobustMultiProvider, get_robust_ws_provider, safe_ws_call
from src.core.circuit_breaker import async_breaker, ws_breaker
from src.monitoring.metrics import (
    init_rpc_connection_status_gauges,
    record_rpc_provider_failure,
    set_rpc_connection_status,
)
from web3 import AsyncWeb3

load_dotenv()

logger = logging.getLogger(__name__)

POLL_SEC = float(os.getenv("MONITOR_POLL_SEC", "12"))
BACKOFF_BASE_SEC = float(os.getenv("MONITOR_WS_BACKOFF_BASE_SEC", "5"))
BACKOFF_MAX_SEC = float(os.getenv("MONITOR_WS_BACKOFF_MAX_SEC", "300"))


def _exponential_backoff_sec(failures: int) -> float:
    """Exponential pause after transient errors (capped)."""
    if failures <= 0:
        return BACKOFF_BASE_SEC
    return min(BACKOFF_MAX_SEC, BACKOFF_BASE_SEC * (2 ** min(failures - 1, 6)))


def _breaker_backoff_sec() -> float:
    """Pause when the WS circuit breaker is open (capped)."""
    return min(BACKOFF_MAX_SEC, float(ws_breaker.reset_timeout) * 2)


async def monitor_loop(ws_w3: AsyncWeb3) -> None:
    """Poll chain head while the WS session stays open."""
    provider = get_robust_ws_provider()
    while True:
        block = await safe_ws_call(lambda: ws_w3.eth.block_number())
        set_rpc_connection_status(provider.current, True)
        logger.info(
            "WS connected | provider=%s block=%s",
            provider.current,
            block,
        )
        await asyncio.sleep(POLL_SEC)


@async_breaker(ws_breaker)
async def _run_ws_session(provider: RobustMultiProvider) -> None:
    async with await provider.get_ws_provider() as ws_w3:
        logger.info("Monitor started with robust WS (breaker=%s)", ws_breaker.current_state)
        await monitor_loop(ws_w3)


async def _run_monitor() -> None:
    provider = get_robust_ws_provider()
    init_rpc_connection_status_gauges(list(provider.providers.keys()))
    failures = 0

    while True:
        try:
            await _run_ws_session(provider)
            failures = 0
        except pybreaker.CircuitBreakerError:
            failures += 1
            name = provider.current
            record_rpc_provider_failure(name)
            set_rpc_connection_status(name, False)
            pause = _breaker_backoff_sec()
            logger.warning(
                "WS circuit breaker OPEN; provider=%s failures=%s pausing %.0fs "
                "(fail_max=%s reset_timeout=%ss)",
                name,
                failures,
                pause,
                ws_breaker.fail_max,
                ws_breaker.reset_timeout,
            )
            await asyncio.sleep(pause)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            provider.record_failure()
            name = provider.current
            record_rpc_provider_failure(name)
            set_rpc_connection_status(name, False)
            await maybe_dispatch_rpc_failure_alert(exc, provider=name)
            pause = _exponential_backoff_sec(failures)
            logger.warning(
                "Monitor loop error: %s | provider=%s failures=%s retry in %.0fs",
                exc,
                name,
                failures,
                pause,
            )
            await asyncio.sleep(pause)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    logger.info(
        "Monitor starting (ws_breaker fail_max=%s reset_timeout=%ss backoff_max=%ss)",
        ws_breaker.fail_max,
        ws_breaker.reset_timeout,
        BACKOFF_MAX_SEC,
    )
    await _run_monitor()


if __name__ == "__main__":
    asyncio.run(main())
