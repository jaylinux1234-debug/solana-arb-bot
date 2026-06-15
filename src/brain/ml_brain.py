# src/brain/ml_brain.py
"""
ML + AI brain for opportunity approval and strategy routing.

Delegates to ``src.core.ai_decision`` when confidence thresholds are tight;
uses aggressive heuristics for fill-mode (``AI_APPROVE_MIN_CONFIDENCE <= 52``).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.config.settings import get_settings

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


async def ai_approve_opportunity(opp: dict[str, Any]) -> bool:
    """Approve a trade opportunity via AI/heuristic gate."""
    settings = get_settings()
    min_conf = int(getattr(settings, "AI_APPROVE_MIN_CONFIDENCE", 72) or 72)
    gross = float(opp.get("gross_bps") or 0)
    net = float(opp.get("net_bps") or 0)

    if min_conf <= 52:
        if gross >= float(getattr(settings, "CEX_DEX_MIN_GROSS_SPREAD_BPS", 6)):
            return True
        return net >= float(getattr(settings, "V2_MIN_NET_BPS", 1.0))

    if gross >= 25.0:
        return True

    if not _env_bool("ENABLE_AI_CYCLE_BRAIN", getattr(settings, "ENABLE_AI_CYCLE_BRAIN", True)):
        return net >= float(getattr(settings, "V2_MIN_NET_BPS", 1.0))

    try:
        from src.core.ai_decision import enhanced_ai_approve

        approved, confidence = await enhanced_ai_approve(opp, min_conf=min_conf)
        if not approved:
            logger.info(
                "ML brain reject | conf=%.1f gross=%.2f net=%.2f",
                confidence,
                gross,
                net,
            )
        return bool(approved)
    except Exception as exc:
        logger.debug("ML brain fallback heuristic: %s", exc)
        return net >= float(getattr(settings, "V2_MIN_NET_BPS", 1.0)) and gross >= 6.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


async def route_strategy(opps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Priority routing — collateral first when carry is strong; else STRATEGY_PRIORITY_ORDER."""
    if not opps:
        return None

    settings = get_settings()
    priority = (
        os.getenv("STRATEGY_PRIORITY_ORDER", "collateral_swap,dex_cex_reverse,backrun")
        .replace("[", "")
        .replace("]", "")
        .replace('"', "")
        .split(",")
    )
    order = [s.strip().lower() for s in priority if s.strip()]
    min_collateral_carry = _env_float(
        "COLLATERAL_MIN_NET_BPS",
        float(getattr(settings, "COLLATERAL_MIN_NET_BPS", 25) or 25),
    )

    def _score(opp: dict[str, Any]) -> tuple[int, float, float]:
        lane = str(opp.get("strategy") or opp.get("lane") or "").lower()
        try:
            rank = order.index(lane) if lane in order else len(order)
        except ValueError:
            rank = len(order)
        net = float(opp.get("net_bps") or opp.get("spread_bps") or 0)
        gross = float(opp.get("gross_bps") or net)
        # Strong collateral carry overrides reverse/backrun wait
        if lane == "collateral_swap" and net >= min_collateral_carry:
            rank = -1
        return (rank, -net, -gross)

    ranked = sorted(opps, key=_score)
    best = ranked[0]
    logger.debug(
        "ML route | picked=%s net=%.2f gross=%.2f from %d opps",
        best.get("strategy") or best.get("lane"),
        float(best.get("net_bps") or 0),
        float(best.get("gross_bps") or 0),
        len(opps),
    )
    return best
