# src/events/webhook/helius.py
"""Helius webhook bootstrap — FastAPI on :8799 or aiohttp fallback."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from src.config.settings import get_settings

if TYPE_CHECKING:
    from src.config.settings import Settings
    from src.core.risk import RiskEngine
    from src.strategies.cex_dex_strategy import CexDexStrategy

logger = logging.getLogger(__name__)

_settings: Settings | None = None
_risk: RiskEngine | None = None
_strategy: CexDexStrategy | None = None
_webhook_task: asyncio.Task[None] | None = None


def set_webhook_context(
    bot_settings: Settings,
    bot_risk: RiskEngine,
    bot_strategy: CexDexStrategy,
) -> None:
    """Inject shared bot context for webhook handlers (optional backrun hooks)."""
    global _settings, _risk, _strategy
    _settings = bot_settings
    _risk = bot_risk
    _strategy = bot_strategy


def _log_webhook_task_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Helius webhook server task failed: %s", exc, exc_info=exc)


async def _run_fastapi_webhook(host: str, port: int) -> None:
    """Run Helius FastAPI on HELIUS_WEBHOOK_PORT (use asyncio loop — health uses uvloop)."""
    import sys

    import uvicorn

    from src.execution.helius import build_fastapi_helius_app

    app = build_fastapi_helius_app()
    loop_impl = "asyncio"
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        loop=loop_impl,
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    serve_task.add_done_callback(_log_webhook_task_failure)
    logger.info(
        "Helius FastAPI webhook listening on http://%s:%s (loop=%s, platform=%s)",
        host,
        port,
        loop_impl,
        sys.platform,
    )
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        server.should_exit = True
        serve_task.cancel()
        raise


async def _run_aiohttp_webhook() -> None:
    from src.execution.helius import helius_webhook_server_loop

    await helius_webhook_server_loop()


async def start_helius_webhook(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Start Helius webhook listener in a background task (non-blocking)."""
    global _webhook_task
    settings = get_settings()

    if _webhook_task is not None and not _webhook_task.done():
        logger.debug("Helius webhook already running")
        return

    webhook_port = port or int(os.getenv("HELIUS_WEBHOOK_PORT", "8799"))

    if os.getenv("ENABLE_HELIUS_FASTAPI", "false").lower() in ("1", "true", "yes"):
        _webhook_task = asyncio.create_task(_run_fastapi_webhook(host, webhook_port))
        _webhook_task.add_done_callback(_log_webhook_task_failure)
        logger.info("Helius FastAPI webhook on http://%s:%s", host, webhook_port)
        await _maybe_auto_create_helius_webhook()
        return

    if not settings.ENABLE_HELIUS_WEBHOOK:
        logger.info("Helius webhook disabled (ENABLE_HELIUS_WEBHOOK=false)")
        return

    _webhook_task = asyncio.create_task(_run_aiohttp_webhook())
    _webhook_task.add_done_callback(_log_webhook_task_failure)
    logger.info("Helius aiohttp webhook listener started")
    await _maybe_auto_create_helius_webhook()


async def _maybe_auto_create_helius_webhook() -> None:
    if os.getenv("HELIUS_WEBHOOK_AUTO_CREATE", "false").lower() not in ("1", "true", "yes"):
        return
    await asyncio.sleep(2.0)
    from src.execution.helius import HeliusWebhookListener, resolve_helius_api_key

    registrar = HeliusWebhookListener(None, None, None)  # type: ignore[arg-type]
    api_key = resolve_helius_api_key()
    target = registrar.webhook_target_url()
    if api_key:
        existing = await registrar._find_webhook_by_url(api_key, target)
        if existing:
            registrar.webhook_id = existing
            logger.info("Helius webhook already active id=%s target=%s", existing, target)
            return
    await registrar.create_webhook()


async def stop_helius_webhook() -> None:
    from src.execution.helius import stop_fastapi_helius_uvicorn, stop_helius_webhook_server

    global _webhook_task
    if _webhook_task is not None:
        _webhook_task.cancel()
        try:
            await _webhook_task
        except asyncio.CancelledError:
            pass
        _webhook_task = None
    await stop_helius_webhook_server()
    await stop_fastapi_helius_uvicorn()
