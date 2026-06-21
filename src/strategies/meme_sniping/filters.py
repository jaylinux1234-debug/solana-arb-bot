"""Meme sniping quality + AI gate v2."""

from __future__ import annotations

import logging
from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings
from src.utils.ai import get_ai_decision

logger = logging.getLogger(__name__)


def _social_score(coin: dict[str, Any]) -> int:
    if coin.get("social_score") is not None:
        try:
            return int(coin["social_score"])
        except (TypeError, ValueError):
            pass
    score = 0
    score += min(25, int(coin.get("social_mentions") or 0) * 5)
    score += min(20, int(coin.get("txns_m5_buys") or 0))
    if coin.get("symbol"):
        score += 5
    return score


def _volatility_bps(coin: dict[str, Any]) -> int:
    raw = coin.get("volatility_bps")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    try:
        pc = float(coin.get("price_change_5m") or 0)
        return int(abs(pc) * 100)
    except (TypeError, ValueError):
        return 0


async def should_snipe(token_address: str, coin: dict[str, Any]) -> dict[str, Any]:
    cfg = meme_sniping_settings

    social = _social_score(coin)
    if social < cfg.min_social_score:
        return {
            "approved": False,
            "size_sol": 0.0,
            "confidence": 0,
            "reason": "social_below_min",
        }

    vol_bps = _volatility_bps(coin)
    if vol_bps < cfg.min_volatility_bps:
        return {
            "approved": False,
            "size_sol": 0.0,
            "confidence": 0,
            "reason": "vol_below_min",
        }

    signal: dict[str, Any] = {
        "token_address": token_address,
        "name": coin.get("name"),
        "symbol": coin.get("symbol"),
        "liquidity_usd": coin.get("liquidity"),
        "market_cap": coin.get("market_cap"),
        "price_change_5m": coin.get("price_change_5m", 0),
        "dev_percentage": coin.get("dev_percentage", 0),
        "social_mentions": coin.get("social_mentions", 0),
        "social_score": social,
        "volatility_bps": vol_bps,
        "source": coin.get("source"),
        "evaluation_focus": "volatility + social + safety",
    }

    result = await get_ai_decision(signal, strategy="meme_sniping")
    confidence = float(result.get("confidence") or 0)
    approved = bool(result.get("approve")) and confidence >= cfg.ai_min_confidence
    size_sol = max(0.5, min(cfg.max_trade_sol, confidence / 58.0))

    logger.info(
        "meme_sniping_filter | mint=%s approved=%s confidence=%.1f size_sol=%.3f social=%d vol_bps=%d",
        token_address[:12],
        approved,
        confidence,
        size_sol,
        social,
        vol_bps,
    )

    return {
        "approved": approved,
        "size_sol": size_sol,
        "confidence": confidence,
        "reason": result.get("reason") or "AI Filter",
    }
