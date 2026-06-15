"""Direction regime detection with soft forward allowance in DEX-cheap markets."""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from src.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

MarketRegime = Literal["dex_cheap", "cex_cheap", "neutral"]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class DirectionAwareBrain:
    """
    Regime-aware forward-lane policy: deprioritize (not hard-block) CEX-DEX in dex_cheap
    when gross and confidence are strong.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def detect_regime(
        self,
        signal: dict[str, Any],
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> MarketRegime:
        """Classify market as dex_cheap, cex_cheap, or neutral."""
        direction = str(signal.get("direction") or "").lower()
        if direction == "dex_cheap":
            return "dex_cheap"
        if direction == "cex_cheap":
            return "cex_cheap"

        snap = snapshot or {}
        rev = snap.get("dex_cex_reverse") if isinstance(snap.get("dex_cex_reverse"), dict) else {}
        if rev.get("is_dex_cheap") or str(rev.get("direction", "")).lower() == "dex_cheap":
            return "dex_cheap"

        cex = snap.get("cex_dex") if isinstance(snap.get("cex_dex"), dict) else {}
        if cex.get("is_cex_cheap") or str(cex.get("direction", "")).lower() == "cex_cheap":
            return "cex_cheap"

        return "neutral"

    def evaluate(
        self,
        signal: dict[str, Any],
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> bool:
        """
        Return True to allow forward evaluation; False when hard-blocked.

        In ``dex_cheap`` regime, strong gross + confidence → soft forward (priority medium).
        """
        regime = self.detect_regime(signal, snapshot=snapshot)

        if regime != "dex_cheap":
            return True

        gross = float(
            signal.get("gross_bps") or signal.get("edge_bps") or signal.get("spread_bps_gross") or 0
        )
        ai_conf = float(
            signal.get("ai_confidence")
            or signal.get("ai_conf")
            or signal.get("confidence")
            or 0
        )
        net_bps = float(signal.get("net_bps") or 0)

        min_gross = _env_float("BRAIN_SOFT_FORWARD_MIN_GROSS_BPS", 12.0)
        min_ai = _env_float("BRAIN_SOFT_FORWARD_MIN_AI_CONF", 65.0)
        min_net = float(
            getattr(self.settings, "CEX_DEX_MIN_NET_SPREAD_BPS", 3)
        )

        strong_ai = ai_conf > min_ai
        strong_net = net_bps >= min_net and gross > min_gross * 0.85
        strong_gross = gross > min_gross

        if strong_gross and (strong_ai or strong_net):
            signal["priority"] = "medium"
            signal["soft_forward_allow"] = True
            logger.info(
                "SOFT_FORWARD_ALLOW | gross=%.1f ai=%.1f net=%.1f reason=strong_signal_in_dex_cheap",
                gross,
                ai_conf,
                net_bps,
            )
            return True

        signal["block_reason"] = "wrong_direction_dex_cheap"
        return False

    def soft_forward_score_boost(self) -> float:
        """Extra lane score for cex_dex when soft-forward is active."""
        return _env_float("BRAIN_SOFT_FORWARD_SCORE_BOOST", 18.0)
