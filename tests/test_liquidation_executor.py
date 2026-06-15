"""Liquidation executor hardened profit model tests."""

from __future__ import annotations

import pytest

from src.strategies.liquidation_executor import LiquidationExecutor


def test_estimate_liquidation_profit_realistic(monkeypatch):
    monkeypatch.setenv("LIQUIDATION_BONUS_PCT", "0.05")
    monkeypatch.setenv("LIQUIDATION_NET_RETAIN_FRAC", "0.65")
    ex = LiquidationExecutor()
    obl = {"debt_usdc": 100.0, "liquidation_bonus": 0.05}
    # gross = 5.0, net = 3.25
    assert ex._estimate_liquidation_profit(obl) == pytest.approx(3.25, rel=0.01)


def test_estimate_liquidation_profit_from_micro_amount(monkeypatch):
    monkeypatch.setenv("LIQUIDATION_BONUS_PCT", "0.05")
    monkeypatch.setenv("LIQUIDATION_NET_RETAIN_FRAC", "0.65")
    ex = LiquidationExecutor()
    obl = {"debt_amount": 25_000_000}
    assert ex._estimate_liquidation_profit(obl) == pytest.approx(0.8125, rel=0.01)


def test_estimate_liquidation_profit_zero_debt():
    ex = LiquidationExecutor()
    assert ex._estimate_liquidation_profit({}) == 0.0
