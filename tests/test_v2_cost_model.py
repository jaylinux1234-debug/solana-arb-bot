"""v2.4.2 cost model."""

from __future__ import annotations

from src.v2.config import V2Config
from src.v2.cost_model import CostModel


def test_cost_model_size_and_vol():
    model = CostModel(
        base_cost_bps=5.5,
        jito_tip_bps=1.1,
        withdrawal_buffer_bps=2.0,
        size_impact_linear_bps=1.8,
        size_ref_usdc=15.0,
        size_impact_exponent=1.25,
        vol_penalty_threshold_pct=0.6,
        vol_penalty_slope=8.0,
    )
    net_low_vol = model.calculate_net_bps(12.0, 10.0, vol_pct=0.5)
    net_high_vol = model.calculate_net_bps(12.0, 10.0, vol_pct=1.2)
    assert net_high_vol < net_low_vol
    assert model.get_execution_slippage_bps(10.0) == 53


def test_cost_model_from_config():
    cfg = V2Config(
        base_cost_bps=5.5,
        slippage_buffer_bps=2.0,
        jito_tip_bps=1.1,
    )
    model = CostModel.from_config(cfg)
    assert model.base_cost_bps == 5.5
    assert model.calculate_net_bps(10.0, 10.0) < 10.0
