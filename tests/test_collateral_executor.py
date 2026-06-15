"""Collateral cross-market carry model tests."""

from __future__ import annotations

import pytest

from src.strategies.collateral_executor import CollateralExecutor, _normalize_apy


def test_normalize_apy_percent_scale():
    assert _normalize_apy(8.5) == pytest.approx(0.085, rel=0.01)
    assert _normalize_apy(0.085) == pytest.approx(0.085, rel=0.01)


def test_calculate_net_carry_positive(monkeypatch):
    monkeypatch.setenv("COLLATERAL_FLASH_FEE_BPS", "5")
    monkeypatch.setenv("COLLATERAL_SWAP_SLIPPAGE_BPS", "8")
    monkeypatch.setenv("COLLATERAL_JITO_TIP_BPS", "8")
    ex = CollateralExecutor()
    borrow = {"borrow_apy": 0.04}
    supply = {"supply_apy": 0.10}
    carry = ex._calculate_net_carry(borrow, supply, size_usdc_micro=25_000_000)
    # gross = (0.10 - 0.04) * 10000 = 600; net = 600 - 21 = 579
    assert carry["gross_bps"] == pytest.approx(600.0, rel=0.01)
    assert carry["net_bps"] == pytest.approx(579.0, rel=0.01)
    assert carry["profit_usd"] == pytest.approx(1.4475, rel=0.01)


def test_calculate_net_carry_negative():
    ex = CollateralExecutor()
    borrow = {"borrow_apy": 0.12}
    supply = {"supply_apy": 0.05}
    carry = ex._calculate_net_carry(borrow, supply)
    assert carry["net_bps"] == 0.0
    assert carry["gross_bps"] < 0
