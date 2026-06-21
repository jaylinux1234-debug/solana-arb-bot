"""Meme sniping quality + validator + ensemble + AI gate v3."""

from __future__ import annotations

import logging
from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings
from src.strategies.meme_sniping.position import calculate_position_size, get_available_sol_balance
from src.strategies.meme_sniping.scoring import blend_confidence, ensemble_score
from src.strategies.meme_sniping.validator import validate_token
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

    if token_address in cfg.blacklist_tokens:
        return {
            "approved": False,
            "size_sol": 0.0,
            "confidence": 0,
            "reason": "blacklisted",
        }

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

    validation = await validate_token(token_address, coin)
    if not validation["passed"]:
        return {
            "approved": False,
            "size_sol": 0.0,
            "confidence": 0,
            "reason": "validator_failed",
            "validation": validation,
        }

    signal: dict[str, Any] = {
        "token_address": token_address,
        "name": coin.get("name"),
        "symbol": coin.get("symbol"),
        "liquidity_usd": coin.get("liquidity"),
        "market_cap": coin.get("market_cap"),
        "price_change_5m": coin.get("price_change_5m", 0),
        "dev_percentage": validation.get("dev_wallet_pct"),
        "dev_wallet_pct": validation.get("dev_wallet_pct"),
        "social_mentions": coin.get("social_mentions", 0),
        "social_score": social,
        "volatility_bps": vol_bps,
        "holder_count": validation.get("holder_count"),
        "sell_tax_pct": validation.get("sell_tax_pct"),
        "safety_score": validation.get("safety_score"),
        "failed_checks": validation.get("failed_checks"),
        "source": coin.get("source"),
        "evaluation_focus": "volatility + social + safety + anti-rug",
    }

    ensemble = ensemble_score(signal)
    signal["ensemble_score"] = round(ensemble, 1)

    result = await get_ai_decision(signal, strategy="meme_sniping")
    ai_confidence = float(result.get("confidence") or 0)
    confidence = blend_confidence(ai_confidence, ensemble)

    approved = (
        bool(result.get("approve"))
        and confidence >= cfg.ai_min_confidence
        and ensemble >= cfg.ensemble_min_score
    )

    sol_balance = await get_available_sol_balance()
    size_sol = calculate_position_size(confidence, sol_balance, vol_bps=vol_bps) if approved else 0.0
    reason = str(result.get("reason") or ("approved" if approved else "ai_rejected"))

    logger.info(
        "meme_sniping_filter | mint=%s approved=%s ai=%.1f ensemble=%.1f blend=%.1f "
        "size_sol=%.3f social=%d vol_bps=%d safety=%.1f",
        token_address[:12],
        approved,
        ai_confidence,
        ensemble,
        confidence,
        size_sol,
        social,
        vol_bps,
        float(validation.get("safety_score") or 0),
    )

    return {
        "approved": approved,
        "size_sol": size_sol,
        "confidence": confidence,
        "ai_confidence": ai_confidence,
        "ensemble_score": ensemble,
        "reason": reason,
        "validation": validation,
    }
