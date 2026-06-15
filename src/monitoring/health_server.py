"""Lightweight HTTP health + Prometheus metrics for Docker / compose."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

_start_ts = time.time()
_redis_ok: bool | None = None

try:
    from prometheus_client import Gauge

    _BOT_UP = Gauge("solana_bot_up", "Bot process is running")
    _BOT_UP.set(1)
except ImportError:
    Gauge = None  # type: ignore[misc, assignment]

# Register RPC counters so they appear on /metrics when the health server runs.
from src.monitoring.metrics import (  # noqa: F401, E402
    rpc_connection_status,
    rpc_provider_failures_total,
)


def set_redis_status(ok: bool | None) -> None:
    global _redis_ok
    _redis_ok = ok


def get_redis_status() -> bool | None:
    """Last singleton / Redis probe result (``None`` if not checked yet)."""
    return _redis_ok


async def _handle_health(_request: web.Request) -> web.Response:
    body: dict[str, Any] = {
        "status": "ok",
        "uptime_sec": round(time.time() - _start_ts, 1),
        "test_mode": os.getenv("TEST_MODE", "true"),
    }
    if _redis_ok is not None:
        body["redis"] = "ok" if _redis_ok else "unavailable"
    if _redis_ok is False:
        body["status"] = "degraded"
        return web.json_response(body, status=503)
    return web.json_response(body)


async def _handle_metrics(_request: web.Request) -> web.Response:
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:
        return web.Response(text="prometheus_client not installed\n", status=501)

    return web.Response(
        body=generate_latest(),
        content_type=CONTENT_TYPE_LATEST.split(";")[0].strip(),
    )


async def run_health_server() -> None:
    if os.getenv("ENABLE_BOT_HEALTH_SERVER", "true").lower() not in ("1", "true", "yes"):
        return

    host = os.getenv("BOT_HEALTH_HOST", "0.0.0.0")
    port = int(os.getenv("BOT_HEALTH_PORT", "8000"))

    app = web.Application()
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/metrics", _handle_metrics)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Bot health server listening on http://%s:%s (/health, /metrics)", host, port)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await runner.cleanup()
        raise


def start_health_server_background() -> asyncio.Task[None]:
    return asyncio.create_task(run_health_server())


# Alias for ``src.main`` and scripts that ``create_task(start_health_server())``.
start_health_server = run_health_server
