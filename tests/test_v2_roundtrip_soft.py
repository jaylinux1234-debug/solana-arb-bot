"""Roundtrip soft-pass, safety buffer, and ledger error mapping."""

from __future__ import annotations

import pytest

from src.v2.config import V2Config
from src.v2.dex_cex_reverse import V2ReverseLane
from src.v2.gates import _roundtrip_slippage_bps, adaptive_detect_min_gross


def test_roundtrip_slippage_scales_with_size():
    cfg = V2Config(execution_slippage_bps=55)
    small = _roundtrip_slippage_bps(cfg, 10.0)
    large = _roundtrip_slippage_bps(cfg, 50.0)
    assert small >= 55
    assert large > small


def test_adaptive_detect_min_gross():
    cfg = V2Config(min_gross_bps_base=1.4)
    assert adaptive_detect_min_gross(cfg, 0.8) >= 1.4


def test_map_ledger_errors():
    assert (
        V2ReverseLane._map_ledger_error("Ledger HTTP 500: 0x6a81")
        == "LEDGER_BLIND_SIGNING_REQUIRED_OR_REJECTED"
    )
    assert (
        V2ReverseLane._map_ledger_error("ECONNREFUSED")
        == "LEDGER_BRIDGE_DOWN_START_LEDGER_BRIDGE"
    )


@pytest.mark.asyncio
async def test_roundtrip_strict_pass_with_safety_buffer(monkeypatch):
    from src.v2 import gates

    monkeypatch.setenv("V2_ROUNDTRIP_NET_SAFETY_MULT", "1.2")
    monkeypatch.setenv("V2_ROUNDTRIP_RETAIN_CHECK", "false")

    cfg = V2Config(min_net_bps=0.2, execution_slippage_bps=55)

    class FakeJupiter:
        async def get_implied_usdc_per_base(self, *args, **kwargs):
            return 69.0, {}

    opp = {
        "cex_bid": 69.5,
        "gross_bps": 15.0,
        "scan_gross_bps": 15.0,
        "net_bps": 0.5,
        "min_net_bps": 0.2,
        "size_usdc_micro": 10_000_000,
        "size_usdc": 10.0,
        "vol_pct": 0.5,
    }

    class FakeModel:
        def calculate_net_bps(self, gross_bps, size_usdc, vol_pct=0.0, **kwargs):
            return 0.3

        def get_execution_slippage_bps(self, size_usdc: float) -> int:
            return 55

    monkeypatch.setattr(
        gates,
        "CostModel",
        type("CM", (), {"from_config": staticmethod(lambda _c: FakeModel())}),
    )
    monkeypatch.setenv("GO_LIVE_SMALL_ACCOUNT", "false")

    ok, reason, rt_net = await gates.check_roundtrip_quote(FakeJupiter(), opp, cfg)
    assert ok is True
    assert reason == "roundtrip_ok"
    assert rt_net == 0.3
    assert opp["roundtrip_jup_price"] == 69.0


@pytest.mark.asyncio
async def test_roundtrip_rejects_scan_net_pass_false_positive(monkeypatch):
    """Roundtrip net below safety must not pass on scan_net alone."""
    from src.v2 import gates

    monkeypatch.setenv("V2_ROUNDTRIP_NET_SAFETY_MULT", "1.2")
    monkeypatch.setenv("V2_ROUNDTRIP_RETAIN_CHECK", "false")

    cfg = V2Config(min_net_bps=0.2, execution_slippage_bps=55)

    class FakeJupiter:
        async def get_implied_usdc_per_base(self, *args, **kwargs):
            return 69.0, {}

    opp = {
        "cex_bid": 69.5,
        "gross_bps": 15.0,
        "scan_gross_bps": 15.0,
        "net_bps": 0.5,
        "min_net_bps": 0.2,
        "size_usdc_micro": 10_000_000,
        "size_usdc": 10.0,
        "vol_pct": 0.5,
    }

    class FakeModel:
        def calculate_net_bps(self, gross_bps, size_usdc, vol_pct=0.0, **kwargs):
            return 0.1

        def get_execution_slippage_bps(self, size_usdc: float) -> int:
            return 55

    monkeypatch.setattr(
        gates,
        "CostModel",
        type("CM", (), {"from_config": staticmethod(lambda _c: FakeModel())}),
    )
    monkeypatch.setenv("GO_LIVE_SMALL_ACCOUNT", "false")

    ok, reason, rt_net = await gates.check_roundtrip_quote(FakeJupiter(), opp, cfg)
    assert ok is False
    assert "roundtrip_net_below" in reason
    assert rt_net == 0.1


@pytest.mark.asyncio
async def test_roundtrip_soft_pass_near_threshold(monkeypatch):
    from src.v2 import gates

    monkeypatch.setenv("V2_ROUNDTRIP_NET_SAFETY_MULT", "1.2")
    monkeypatch.setenv("V2_ROUNDTRIP_RETAIN_CHECK", "false")
    monkeypatch.setenv("V2_ROUNDTRIP_SOFT_PASS_FACTOR", "0.85")

    cfg = V2Config(min_net_bps=0.2, execution_slippage_bps=55)

    class FakeJupiter:
        async def get_implied_usdc_per_base(self, *args, **kwargs):
            return 69.0, {}

    opp = {
        "cex_bid": 69.5,
        "gross_bps": 15.0,
        "scan_gross_bps": 15.0,
        "net_bps": 0.5,
        "min_net_bps": 0.2,
        "size_usdc_micro": 10_000_000,
        "size_usdc": 10.0,
        "vol_pct": 0.5,
    }

    class FakeModel:
        def calculate_net_bps(self, gross_bps, size_usdc, vol_pct=0.0, **kwargs):
            return 0.18

        def get_execution_slippage_bps(self, size_usdc: float) -> int:
            return 55

    monkeypatch.setattr(
        gates,
        "CostModel",
        type("CM", (), {"from_config": staticmethod(lambda _c: FakeModel())}),
    )
    monkeypatch.setenv("GO_LIVE_SMALL_ACCOUNT", "false")

    ok, reason, rt_net = await gates.check_roundtrip_quote(FakeJupiter(), opp, cfg)
    assert ok is True
    assert reason == "roundtrip_soft_pass"
    assert rt_net == 0.18
