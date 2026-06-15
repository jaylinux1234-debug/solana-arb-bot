"""Backrun executor and cost model estimate tests."""

from __future__ import annotations

from src.core.cost_model import AdvancedCostModel


def test_estimate_backrun_positive_gross(monkeypatch):
    monkeypatch.setenv("BACKRUN_EXTRA_JUPITER_LEG_BPS", "10")
    model = AdvancedCostModel(
        {
            "cex_fee_roundtrip": 0.0,
            "jupiter_buffer": 5.0,
            "exec_slippage_base": 10.0,
            "kamino_flash": 0.0,
            "jito_tip": 0.0,
            "vol_penalty_slope": 0.0,
            "inventory_penalty_bps": 0.0,
            "withdrawal_latency_bps_fixed": 5.0,
        }
    )
    quotes = {
        "quote3_sol_to_usdc": {"outAmount": "36000000"},
    }
    est = model.estimate_backrun(quotes, 35_000_000)
    assert est.gross_bps > 0
    assert est.usdc_out_micro == 36_000_000
    assert est.net_bps < est.gross_bps


def test_estimate_backrun_empty_quote():
    model = AdvancedCostModel()
    est = model.estimate_backrun({}, 35_000_000)
    assert est.gross_bps == 0.0
    assert est.profit_usd == 0.0
