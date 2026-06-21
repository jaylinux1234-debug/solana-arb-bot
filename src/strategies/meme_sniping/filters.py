"""Meme sniping quality + AI gate."""

from __future__ import annotations

import logging
from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings
from src.utils.ai import get_ai_decision

logger = logging.getLogger(__name__)


def _coin_signal(coin_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": coin_data.get("name"),
        "mint": coin_data.get("mint"),
        "liquidity_usd": coin_data.get("liquidity"),
        "market_cap": coin_data.get("market_cap"),
        "price_change_5m": coin_data.get("price_change_5m", 0),
        "dev_percentage": coin_data.get("dev_percentage", 0),
        "social_mentions": coin_data.get("social_mentions", 0),
        "social_score": coin_data.get("social_score", 0),
        "volatility_bps": coin_data.get("volatility_bps", 0),
    }


async def should_snipe(token_address: str, coin_data: dict[str, Any]) -> dict[str, Any]:
    cfg = meme_sniping_settings
    signal = _coin_signal(coin_data)
    signal["token_address"] = token_address

    liquidity = float(signal.get("liquidity_usd") or 0.0)
    if liquidity < cfg.min_liquidity_usd:
        return {
            "approved": False,
            "size_sol": 0.0,
            "confidence": 0,
            "reason": "liquidity_below_min",
        }

    social_score = int(signal.get("social_score") or 0)
    if social_score and social_score < cfg.min_social_score:
        return {
            "approved": False,
            "size_sol": 0.0,
            "confidence": 0,
            "reason": "social_score_below_min",
        }

    ai = await get_ai_decision(signal, strategy="meme_sniping")
    confidence = float(ai.get("confidence") or 0)
    approved = bool(ai.get("approve")) and confidence >= cfg.ai_min_confidence
    size = max(0.6, min(cfg.max_trade_sol, confidence / 55.0))

    logger.info(
        "meme_sniping_filter | mint=%s approved=%s confidence=%.1f size_sol=%.3f reason=%s",
        token_address[:12],
        approved,
        confidence,
        size,
        ai.get("reason") or "ai_gate",
    )

    return {
        "approved": approved,
        "size_sol": size,
        "confidence": confidence,
        "reason": ai.get("reason") or "AI + Filters",
    }
