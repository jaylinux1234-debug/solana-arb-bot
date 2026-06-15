"""Operational alerts (structured logging only)."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from src.events.bus import get_event_bus
from src.events.types import BotEvent, EventKind

logger = logging.getLogger(__name__)

_last_rpc_critical_alert_ts = 0.0


def is_rpc_critical_error(error: BaseException | str) -> bool:
    """True when error text indicates rate-limit or WebSocket failure."""
    text = str(error)
    lower = text.lower()
    return (
        "429" in text
        or "WebSocket" in text
        or "websocket" in lower
        or "providerconnectionerror" in lower
    )


async def dispatch_alert(level: str, message: str) -> None:
    """Log an operational alert. ``level``: CRITICAL, WARN, INFO."""
    lvl = (level or "INFO").strip().upper()
    msg = (message or "").strip()
    if lvl == "CRITICAL":
        logger.critical("ALERT [%s]: %s", lvl, msg)
    elif lvl == "WARN":
        logger.warning("ALERT [%s]: %s", lvl, msg)
    else:
        logger.info("ALERT [%s]: %s", lvl, msg)
    get_event_bus().publish_fire_and_forget(
        BotEvent(kind=EventKind.ALERT, source="monitoring", data={"level": lvl, "message": msg})
    )


def schedule_alert(message: str, *, level: str = "CRITICAL") -> None:
    """Fire-and-forget :func:`dispatch_alert` from sync or async contexts."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(dispatch_alert(level, message))
    except RuntimeError:
        try:
            asyncio.run(dispatch_alert(level, message))
        except Exception as exc:
            logger.warning("Alert dispatch (sync fallback) failed: %s", exc)


async def maybe_dispatch_rpc_failure_alert(
    error: BaseException | str,
    *,
    provider: str = "",
) -> None:
    """Rate-limited CRITICAL alert for RPC / WebSocket failures."""
    global _last_rpc_critical_alert_ts

    if not is_rpc_critical_error(error):
        return

    from src.monitoring.metrics import set_rpc_connection_status

    name = (provider or "unknown").strip().lower() or "unknown"
    set_rpc_connection_status(name, False)

    cooldown = float(os.getenv("RPC_ALERT_COOLDOWN_SEC", "300"))
    now = time.time()
    if now - _last_rpc_critical_alert_ts < cooldown:
        logger.debug("RPC alert suppressed (cooldown %.0fs)", cooldown)
        return

    _last_rpc_critical_alert_ts = now
    detail = str(error)[:500]
    get_event_bus().publish_fire_and_forget(
        BotEvent(
            kind=EventKind.RPC_FAILURE,
            source=name,
            data={"detail": detail},
        )
    )
    await dispatch_alert(
        "CRITICAL",
        f"RPC connection dead - manual intervention required ({name}: {detail})",
    )


def schedule_rpc_failure_alert(
    error: BaseException | str,
    *,
    provider: str = "",
) -> None:
    """Fire-and-forget :func:`maybe_dispatch_rpc_failure_alert`."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(maybe_dispatch_rpc_failure_alert(error, provider=provider))
    except RuntimeError:
        try:
            asyncio.run(maybe_dispatch_rpc_failure_alert(error, provider=provider))
        except Exception as exc:
            logger.warning("RPC alert (sync fallback) failed: %s", exc)
