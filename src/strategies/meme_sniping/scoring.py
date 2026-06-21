"""Ensemble scoring for meme sniping (on-chain + social + momentum)."""

from __future__ import annotations

from typing import Any


def _rug_score(token_data: dict[str, Any]) -> float:
    dev = float(token_data.get("dev_wallet_pct") or token_data.get("dev_percentage") or 0)
    failed = token_data.get("failed_checks") or []
    penalty = len(failed) * 12.0
    dev_penalty = min(50.0, dev * 2.5)
    return min(100.0, dev_penalty + penalty)


def calculate_momentum(token_data: dict[str, Any]) -> float:
    try:
        pc5 = abs(float(token_data.get("price_change_5m") or 0))
        buys = int(token_data.get("txns_m5_buys") or 0)
        return min(100.0, pc5 * 4.0 + buys * 2.5)
    except (TypeError, ValueError):
        return 0.0


def ensemble_score(token_data: dict[str, Any]) -> float:
    """Combine on-chain + social + momentum features."""
    vol_bps = float(token_data.get("volatility_bps") or token_data.get("vol_bps") or 0)
    liq_usd = float(token_data.get("liquidity_usd") or token_data.get("liquidity") or 0)
    social = float(token_data.get("social_score") or 0)

    features = {
        "vol": min(100.0, vol_bps / 10.0),
        "liq": min(100.0, liq_usd / 300.0),
        "social": min(100.0, social * 1.8),
        "momentum": calculate_momentum(token_data),
        "rug": max(0.0, 100.0 - _rug_score(token_data)),
    }
    weights = {
        "vol": 0.25,
        "liq": 0.30,
        "social": 0.20,
        "momentum": 0.15,
        "rug": 0.10,
    }
    return sum(features[k] * weights[k] for k in weights)


def blend_confidence(ai_confidence: float, ensemble: float) -> float:
    """Weighted blend of AI and ensemble scores."""
    return 0.60 * float(ai_confidence) + 0.40 * float(ensemble)
