"""Telegram bot alerts for operational risk events."""

from __future__ import annotations

import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

_last_sent: dict[str, float] = {}


def _configured() -> bool:
    return bool((os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()) and bool(
        (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    )


async def send_telegram(message: str, *, parse_mode: str = "") -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return False

    text = (message or "").strip()[:4000]
    if not text:
        return False

    try:
        import httpx
    except ImportError:
        logger.debug("httpx not available for telegram")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            logger.warning("Telegram HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
    return False


def schedule_telegram(message: str, *, dedupe_key: str = "", cooldown_sec: float = 300.0) -> None:
    """Fire-and-forget Telegram with optional dedupe cooldown."""
    if not _configured():
        return

    now = time.time()
    if dedupe_key:
        last = _last_sent.get(dedupe_key, 0.0)
        if now - last < cooldown_sec:
            return
        _last_sent[dedupe_key] = now

    async def _run() -> None:
        await send_telegram(message)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
    except RuntimeError:
        try:
            asyncio.run(_run())
        except Exception as exc:
            logger.debug("telegram sync fallback: %s", exc)
