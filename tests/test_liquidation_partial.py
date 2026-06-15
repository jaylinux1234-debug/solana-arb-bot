"""LIQUIDATION_PARTIAL_PCT sizing tests."""

from __future__ import annotations

from src.strategies.liquidation_executor import LiquidationExecutor


def test_partial_liquidity_amount_half_debt():
    assert LiquidationExecutor.partial_liquidity_amount(10_000_000, 35_000_000, partial_pct=0.5) == 5_000_000


def test_partial_liquidity_amount_capped_by_flash():
    assert LiquidationExecutor.partial_liquidity_amount(100_000_000, 35_000_000, partial_pct=0.5) == 35_000_000


def test_partial_liquidity_amount_no_debt_uses_flash():
    assert LiquidationExecutor.partial_liquidity_amount(0, 35_000_000, partial_pct=0.5) == 35_000_000
