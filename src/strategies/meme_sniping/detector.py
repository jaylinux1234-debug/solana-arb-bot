"""Pump.fun detector v2 — faster poll, optional Alchemy-backed reads."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from src.strategies.meme_sniping.config import meme_sniping_settings
from src.strategies.meme_sniping.execution import execute_snipe
from src.strategies.meme_sniping.filters import should_snipe

logger = logging.getLogger(__name__)

_PUMP_FUN_URL = "https://api.pump.fun/coins?limit=30"
_seen_mints: set[str] = set()


def _alchemy_rpc_hint() -> str:
    if not meme_sniping_settings.use_alchemy:
        return "alchemy=off"
    url = (os.getenv("SOLANA_RPC_URL") or "").lower()
    if "alchemy.com" in url:
        return "alchemy=primary"
    return "alchemy=env_flag_on"


async def detect_new_pools(shutdown_event: asyncio.Event | None = None) -> None:
    cfg = meme_sniping_settings
    logger.info(
        "Meme sniping detector v2 started | simulate=%s %s min_liq=%.0f ai_conf=%.0f",
        cfg.simulate,
        _alchemy_rpc_hint(),
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
                        liq = float(coin.get("liquidity") or 0.0)
                        if liq >= cfg.min_liquidity_usd:
                            await process_coin(coin)
            await asyncio.sleep(0.85)
        except Exception as exc:
            logger.error("Meme sniping detector error: %s", exc)
            await asyncio.sleep(2.0)


async def process_coin(coin: dict[str, Any]) -> None:
    try:
        token = str(coin.get("mint") or "").strip()
        if not token or token in _seen_mints:
            return
        _seen_mints.add(token)
        if len(_seen_mints) > 500:
            _seen_mints.clear()

        decision = await should_snipe(token, coin)
        if decision["approved"]:
            logger.info(
                "meme_sniping_strong_signal | name=%s mint=%s confidence=%.1f size_sol=%.3f",
                coin.get("name"),
                token[:12],
                float(decision.get("confidence") or 0),
                float(decision.get("size_sol") or 0),
            )
            await execute_snipe(token, float(decision["size_sol"]))
    except Exception as exc:
        logger.debug("meme_sniping process_coin failed: %s", exc)
