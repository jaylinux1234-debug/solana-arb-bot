"""Volatility gate only tracks SOL prices (meme prices must not pollute vol)."""

from __future__ import annotations

import time

import pytest

from src.strategies.volatility_gate import (
    get_5m_volatility_pct,
    record_cex_price,
)


@pytest.fixture(autouse=True)
def _clear_samples() -> None:
    from src.strategies import volatility_gate as vg

    vg._samples.clear()
    yield
    vg._samples.clear()


def test_record_cex_price_ignores_meme_tokens() -> None:
    record_cex_price(72.0, symbol="SOL")
    record_cex_price(0.000023, symbol="BONK")
    record_cex_price(2.5, symbol="WIF")
    assert get_5m_volatility_pct() is None  # need 2 SOL samples


def test_record_cex_price_sol_range() -> None:
    record_cex_price(72.0, symbol="SOL")
    record_cex_price(72.05, symbol="SOL")
    vol = get_5m_volatility_pct()
    assert vol is not None
    assert vol < 1.0


def test_record_cex_price_rejects_out_of_range_sol() -> None:
    record_cex_price(0.5, symbol="SOL")
    record_cex_price(900.0, symbol="SOL")
    assert get_5m_volatility_pct() is None
