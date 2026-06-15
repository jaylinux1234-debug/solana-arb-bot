"""Advanced roundtrip cost model tests."""

from __future__ import annotations

import pytest

from src.core.cost_model import AdvancedCostModel, RoundtripCost


def test_roundtrip_cost_net_is_gross_minus_components():
    model = AdvancedCostModel(
        {
            "cex_fee_roundtrip": 10.0,
            "jupiter_buffer": 10.0,
            "exec_slippage_base": 20.0,
            "kamino_flash": 0.0,
            "jito_tip": 0.0,
            "withdrawal_latency_sec": 0.0,
            "withdrawal_latency_per_sec": 0.0,
            "vol_penalty_slope": 0.0,
            "size_ref_usdc_micro": 25_000_000,
            "inventory_sol_floor": 0.3,
            "inventory_penalty_bps": 0.0,
        }
    )
    rc = model.calculate_roundtrip(
        gross_bps=50.0,
        trade_usdc=25_000_000,
        vol_5m_pct=0.0,
        wallet_sol=1.0,
        cex_sol=1.0,
        is_reverse_path=False,
    )
    assert isinstance(rc, RoundtripCost)
    assert rc.total_cost_bps == pytest.approx(40.0)
    assert rc.net_bps == pytest.approx(10.0)


def test_reverse_path_uses_fixed_low_withdrawal_latency():
    model = AdvancedCostModel(
        {
            "cex_fee_roundtrip": 0.0,
            "jupiter_buffer": 0.0,
            "exec_slippage_base": 0.0,
            "kamino_flash": 0.0,
            "jito_tip": 0.0,
            "withdrawal_latency_sec": 25.0,
            "withdrawal_latency_per_sec": 1.0,
            "vol_penalty_slope": 0.0,
            "inventory_penalty_bps": 0.0,
        }
    )
    rc = model.calculate_roundtrip(10.0, 25_000_000, is_reverse_path=True)
    assert rc.withdrawal_latency_bps == pytest.approx(5.0)
    assert rc.is_reverse_path is True


def test_size_scales_slippage_component():
    model = AdvancedCostModel(
        {
            "cex_fee_roundtrip": 0.0,
            "jupiter_buffer": 0.0,
            "exec_slippage_base": 40.0,
            "kamino_flash": 0.0,
            "jito_tip": 0.0,
            "withdrawal_latency_sec": 0.0,
            "withdrawal_latency_per_sec": 0.0,
            "vol_penalty_slope": 0.0,
            "size_ref_usdc_micro": 25_000_000,
            "inventory_penalty_bps": 0.0,
        }
    )
    small = model.calculate_roundtrip(20.0, 10_000_000).total_cost_bps
    large = model.calculate_roundtrip(20.0, 50_000_000).total_cost_bps
    assert large > small


def test_inventory_penalty_when_sol_low(monkeypatch):
    monkeypatch.delenv("GO_LIVE_SMALL_ACCOUNT", raising=False)
    model = AdvancedCostModel(
        {
            "inventory_sol_floor": 0.3,
            "inventory_penalty_bps": 12.0,
            "vol_penalty_slope": 0.0,
            "withdrawal_latency_sec": 0.0,
            "withdrawal_latency_per_sec": 0.0,
        }
    )
    healthy = model.calculate_roundtrip(30.0, 25_000_000, wallet_sol=0.2, cex_sol=0.2)
    low = model.calculate_roundtrip(30.0, 25_000_000, wallet_sol=0.05, cex_sol=0.05)
    assert low.inventory_penalty_bps == 12.0
    assert low.net_bps < healthy.net_bps


def test_go_live_small_account_softens_inventory_penalty(monkeypatch):
    monkeypatch.setenv("GO_LIVE_SMALL_ACCOUNT", "true")
    model = AdvancedCostModel()
    rc = model.calculate_roundtrip(
        30.0,
        25_000_000,
        wallet_sol=0.2,
        cex_sol=0.2,
    )
    assert rc.inventory_penalty_bps == 0.0
