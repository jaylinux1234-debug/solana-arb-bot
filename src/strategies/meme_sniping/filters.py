"""Meme sniping quality + AI gate v2."""

from __future__ import annotations

import logging
from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings
from src.utils.ai import get_ai_decision

logger = logging.getLogger(__name__)


async def should_snipe(token_address: str, coin: dict[str, Any]) -> dict[str, Any]:
    cfg = meme_sniping_settings

    signal: dict[str, Any] = {
        "token_address": token_address,
        "name": coin.get("name"),
        "symbol": coin.get("symbol"),
        "liquidity_usd": coin.get("liquidity"),
        "market_cap": coin.get("market_cap"),
        "price_change_5m": coin.get("price_change_5m", 0),
        "dev_percentage": coin.get("dev_percentage", 0),
        "social_mentions": coin.get("social_mentions", 0),
        "volatility_bps": coin.get("volatility_bps", 0),
        "evaluation_focus": "volatility + social + safety",
    }

    vol_bps = int(signal.get("volatility_bps") or 0)
    if vol_bps and vol_bps < cfg.min_volatility_bps:
        return {
            "approved": False,
            "size_sol": 0.0,
            "confidence": 0,
            "reason": "vol_below_min",
        }

    result = await get_ai_decision(signal, strategy="meme_sniping")
    confidence = float(result.get("confidence") or 0)
    approved = bool(result.get("approve")) and confidence >= cfg.ai_min_confidence
    size_sol = max(0.5, min(cfg.max_trade_sol, confidence / 58.0))

    logger.info(
        "meme_sniping_filter | mint=%s approved=%s confidence=%.1f size_sol=%.3f",
        token_address[:12],
        approved,
        confidence,
        size_sol,
    )

    return {
        "approved": approved,
        "size_sol": size_sol,
        "confidence": confidence,
        "reason": result.get("reason") or "AI Filter",
    }
