# src/ai/decision.py
"""AI approval gate — TradeHistoryTrainer + live heuristics."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from src.ai.ensemble_scorer import blend_confidence, build_signal_features, heuristic_confidence, lgbm_confidence
from src.ai.trade_history_trainer import TradeHistoryTrainer

trainer = TradeHistoryTrainer()


def _normalize_signal(signal: dict[str, Any] | Any, size_usdc: int | float) -> dict[str, Any]:
    """Accept dict or strategy signal objects; ensure trainer feature keys."""
    if isinstance(signal, dict):
        out: dict[str, Any] = dict(signal)
    else:
        out = {
            "cex_mid": getattr(signal, "cex_mid", None),
            "cex_price": getattr(signal, "cex_price", None),
            "jup_price": getattr(signal, "jup_price", None),
            "jupiter_price": getattr(signal, "jupiter_price", None),
            "gross_bps": getattr(signal, "gross_bps", 0),
            "net_bps": getattr(signal, "net_bps", 0),
            "volatility_bps": getattr(signal, "volatility_bps", 80),
            "direction": getattr(signal, "direction", "cex_cheap"),
        }

    out["size_usdc"] = float(size_usdc)
    out.setdefault("gross_spread_bps", out.get("gross_bps", 0))
    out.setdefault("base_confidence", 80)
    return out


async def ai_approve_trade(
    signal: dict[str, Any] | Any,
    size_usdc: int | float,
    min_confidence: int = 88,
    **kwargs: Any,
) -> tuple[bool, int, str]:
    """
    Fine-tuned prediction blended with heuristics.

    Returns ``(approved, final_confidence, reason)``.
    """
    _ = kwargs  # e.g. pnl_window_hours from strategy callers
    sig = _normalize_signal(signal, size_usdc)

    feats = build_signal_features(
        gross_bps=float(sig.get("gross_spread_bps") or sig.get("gross_bps") or 0),
        net_bps=float(sig.get("net_bps") or 0),
        size_usdc_micro=int(sig.get("size_usdc_micro") or sig.get("size_usdc") or 0),
        cex_price=float(sig.get("cex_price") or sig.get("cex_mid") or 0),
        jup_price=float(sig.get("jup_price") or sig.get("jupiter_price") or 0),
        volatility_bps=float(sig.get("volatility_bps", 85)),
        cex_depth_util=float(sig.get("cex_depth_util", 0.5)),
        jupiter_impact_pct=float(sig.get("jupiter_impact_pct", 0)),
        jupiter_route_hops=int(sig.get("jupiter_route_hops", 0)),
        cex_bid_ask_spread_bps=float(sig.get("cex_bid_ask_spread_bps", 0)),
    )
    feats["market_regime"] = trainer._classify_regime(feats)

    heur = heuristic_confidence(
        float(feats["gross_spread_bps"]),
        float(sig.get("net_bps") or 0),
        int(sig.get("size_usdc_micro") or sig.get("size_usdc") or 0),
    )
    ml_conf, reason = lgbm_confidence(feats)
    final_confidence = int(
        blend_confidence(heur, ml_conf, ensemble=None)
    )
    final_confidence = max(
        final_confidence,
        int((heur * 0.35) + (float(sig.get("base_confidence", 80)) * 0.15)),
    )

    approved = final_confidence >= min_confidence

    if approved and os.getenv("ML_RETRAIN_ON_APPROVE", "false").lower() in (
        "1",
        "true",
        "yes",
    ):
        await asyncio.to_thread(trainer.train_model)

    return approved, final_confidence, reason
