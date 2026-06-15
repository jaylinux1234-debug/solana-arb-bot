"""Tests for v2.2 adaptive vol gates."""

from __future__ import annotations

from src.v2.config import V2Config
from src.v2.volatility import VolatilityTracker


def test_adaptive_thresholds_scales_with_vol():
    cfg = V2Config(
        min_gross_bps_base=7.0,
        min_net_bps_base=1.2,
        adaptive_vol_enabled=True,
    )
    low_gross, low_net = cfg.adaptive_thresholds_for_vol(0.6)
    high_gross, high_net = cfg.adaptive_thresholds_for_vol(1.2)
    assert low_gross < 7.0
    assert high_gross > 7.0
    # v2.4.1: net min is fixed at base; only gross scales with vol
    assert low_net == high_net == cfg.min_net_bps_base


def test_volatility_tracker_neutral_default():
    tracker = VolatilityTracker(lookback_min=5)
    assert tracker.get_volatility_pct() == 0.8
