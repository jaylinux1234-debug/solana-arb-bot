"""Pump.fun detector v2 — DexScreener fallback when pump.fun is blocked."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings
from src.strategies.meme_sniping.execution import execute_snipe
from src.strategies.meme_sniping.filters import should_snipe
from src.strategies.meme_sniping.metrics import meme_sniping_metrics
from src.strategies.meme_sniping.sources import fetch_candidate_coins

logger = logging.getLogger(__name__)

_mint_last_seen: dict[str, float] = {}
_summary_counter = 0


def _alchemy_rpc_hint() -> str:
    if not meme_sniping_settings.use_alchemy:
        return "alchemy=off"
    url = (os.getenv("SOLANA_RPC_URL") or "").lower()
    if "alchemy.com" in url:
        return "alchemy=primary"
    return "alchemy=env_flag_on"


def _mint_on_cooldown(token: str) -> bool:
    cfg = meme_sniping_settings
    last = _mint_last_seen.get(token)
    if last is None:
        return False
    return (time.monotonic() - last) < (cfg.mint_cooldown_minutes * 60)


def _mark_mint_seen(token: str) -> None:
    _mint_last_seen[token] = time.monotonic()
    if len(_mint_last_seen) > 3000:
        _prune_mint_cooldowns()


def _prune_mint_cooldowns() -> None:
    cfg = meme_sniping_settings
    cutoff = time.monotonic() - (cfg.mint_cooldown_minutes * 60 * 2)
    stale = [mint for mint, seen_at in _mint_last_seen.items() if seen_at < cutoff]
    for mint in stale:
        _mint_last_seen.pop(mint, None)


async def detect_new_pools(shutdown_event: asyncio.Event | None = None) -> None:
    cfg = meme_sniping_settings
    logger.info(
        "Meme sniping detector v3 started | simulate=%s %s min_liq=%.0f ai_conf=%.0f "
        "ensemble_min=%.0f mint_cooldown=%dm stop_grace=%ds trailing=%s",
        cfg.simulate,
        _alchemy_rpc_hint(),
        cfg.min_liquidity_usd,
        cfg.ai_min_confidence,
        cfg.ensemble_min_score,
        cfg.mint_cooldown_minutes,
        cfg.stop_grace_sec,
        cfg.enable_trailing_stop,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("Meme sniping detector stopping (shutdown)")
            return

        try:
            coins, source = await fetch_candidate_coins(limit=15)
            meme_sniping_metrics.record_scan(source, len(coins))

            above_liq = 0
            for coin in coins:
                liq = float(coin.get("liquidity") or 0.0)
                if liq >= cfg.min_liquidity_usd:
                    above_liq += 1
                    await process_coin(coin)

            global _summary_counter
            _summary_counter += 1
            if _summary_counter % 30 == 0:
                logger.info(
                    "meme_sniping_scan | source=%s candidates=%d above_min_liq=%d min_liq=%.0f",
                    source,
                    len(coins),
                    above_liq,
                    cfg.min_liquidity_usd,
                )
            meme_sniping_metrics.log_summary_if_due(interval_sec=300.0)

            await asyncio.sleep(0.85)
        except Exception as exc:
            logger.error("Meme sniping detector error: %s", exc)
            await asyncio.sleep(2.0)


async def process_coin(coin: dict[str, Any]) -> None:
    try:
        token = str(coin.get("mint") or "").strip()
        if not token or _mint_on_cooldown(token):
            return
        _mark_mint_seen(token)

        decision = await should_snipe(token, coin)
        meme_sniping_metrics.record_ai(decision["approved"])
        if not decision["approved"]:
            reason = str(decision.get("reason") or "rejected")
            if len(reason) > 40 or (" " in reason and reason not in (
                "vol_below_min",
                "social_below_min",
                "ai_rejected",
                "validator_failed",
                "blacklisted",
            )):
                reason = "ai_rejected"
            meme_sniping_metrics.record_reject(reason)
            return

        logger.info(
            "meme_sniping_strong_signal | name=%s mint=%s confidence=%.1f size_sol=%.3f source=%s",
            coin.get("name"),
            token[:12],
            float(decision.get("confidence") or 0),
            float(decision.get("size_sol") or 0),
            coin.get("source", "?"),
        )
        meme_sniping_metrics.record_entry(
            token,
            float(decision["size_sol"]),
            float(decision.get("confidence") or 0),
        )
        await execute_snipe(token, float(decision["size_sol"]))
    except Exception as exc:
        logger.debug("meme_sniping process_coin failed: %s", exc)
