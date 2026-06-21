"""Pump.fun pool detector for meme sniping (simulate-first)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from src.strategies.meme_sniping.config import meme_sniping_settings
from src.strategies.meme_sniping.execution import execute_snipe
from src.strategies.meme_sniping.filters import should_snipe

logger = logging.getLogger(__name__)

_PUMP_FUN_URL = "https://api.pump.fun/coins?limit=30&offset=0"
_seen_mints: set[str] = set()


async def detect_new_pools(shutdown_event: asyncio.Event | None = None) -> None:
    """Poll recent pump.fun listings and run snipe filters."""
    cfg = meme_sniping_settings
    logger.info(
        "Meme sniping detector started | simulate=%s min_liq=%.0f ai_conf=%.0f",
        cfg.simulate,
        cfg.min_liquidity_usd,
        cfg.ai_min_confidence,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("Meme sniping detector stopping (shutdown)")
            return

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(_PUMP_FUN_URL)
                if resp.status_code == 200:
                    payload = resp.json()
                    coins = payload if isinstance(payload, list) else payload.get("coins", [])
                    for coin in coins[:15]:
                        if not isinstance(coin, dict):
                            continue
                        await process_coin(coin)
            await asyncio.sleep(1.1)
        except Exception as exc:
            logger.error("Meme sniping detector error: %s", exc)
            await asyncio.sleep(3.0)


async def process_coin(coin: dict[str, Any]) -> None:
    cfg = meme_sniping_settings
    try:
        liquidity = float(coin.get("liquidity") or 0.0)
        market_cap = float(coin.get("market_cap") or 0.0)
        if liquidity < cfg.min_liquidity_usd:
            return
        if market_cap < 18000:
            return

        token = str(coin.get("mint") or "").strip()
        if not token or token in _seen_mints:
            return
        _seen_mints.add(token)
        if len(_seen_mints) > 500:
            _seen_mints.clear()

        logger.info(
            "meme_sniping_candidate | name=%s mint=%s liq=%.0f mcap=%.0f",
            coin.get("name"),
            token[:12],
            liquidity,
            market_cap,
        )

        decision = await should_snipe(token, coin)
        if decision["approved"]:
            await execute_snipe(token, float(decision["size_sol"]))
    except Exception as exc:
        logger.debug("meme_sniping process_coin failed: %s", exc)
