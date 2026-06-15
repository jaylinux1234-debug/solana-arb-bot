"""Tests for v2 static gates."""

from __future__ import annotations

import pytest

from src.v2.config import V2Config
from src.v2.gates import (
    adaptive_min_net_bps,
    check_static_gates,
    resolve_adaptive_thresholds,
)


def test_static_gates_pass_dex_cheap():
    cfg = V2Config(min_gross_bps=12.0, min_net_bps=3.0)
    opp = {
        "direction": "dex_cheap",
        "gross_bps": 15.0,
        "net_bps": 5.0,
    }
    ok, reason, _ = check_static_gates(opp, cfg)
    assert ok is True
    assert reason == "static_ok"


def test_static_gates_reject_low_gross():
    cfg = V2Config(min_gross_bps=12.0, min_net_bps=3.0)
    opp = {"direction": "dex_cheap", "gross_bps": 8.0, "net_bps": 5.0}
    ok, reason, _ = check_static_gates(opp, cfg)
    assert ok is False
    assert "gross_below" in reason


def test_static_gates_reject_not_dex_cheap():
    cfg = V2Config()
    opp = {"direction": "cex_cheap", "gross_bps": 20.0, "net_bps": 10.0}
    ok, reason, _ = check_static_gates(opp, cfg)
    assert ok is False
    assert reason == "not_dex_cheap"


def test_adaptive_min_net_relaxed_when_inventory_healthy():
    cfg = V2Config(min_net_bps_base=0.3)
    assert adaptive_min_net_bps(0.5, True, cfg) == pytest.approx(0.2)
    assert adaptive_min_net_bps(0.5, False, cfg) == pytest.approx(0.3)
    assert adaptive_min_net_bps(1.0, True, cfg) == pytest.approx(0.3)


def test_resolve_adaptive_thresholds_inventory():
    cfg = V2Config(min_gross_bps_base=1.2, min_net_bps_base=0.3)
    gross, net = resolve_adaptive_thresholds(cfg, 0.5, True)
    assert net < cfg.min_net_bps_base
    assert gross >= 1.2
