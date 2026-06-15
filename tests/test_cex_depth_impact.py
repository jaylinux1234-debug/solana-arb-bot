"""CEX order-book walk for roundtrip impact."""

from __future__ import annotations

from src.cex.backpack_ticker import cex_buy_walk_ask_impact_bps


def test_cex_buy_walk_ask_single_level():
    book = {"asks": [["100.0", "1.0"]], "bids": [["99.0", "1.0"]]}
    impact, eff, ok = cex_buy_walk_ask_impact_bps(book, 50.0, max_levels=5)
    assert ok is True
    assert impact == 0.0
    assert eff == 100.0


def test_cex_buy_walk_ask_insufficient_depth():
    book = {"asks": [["100.0", "0.1"]], "bids": [["99.0", "1.0"]]}
    impact, eff, ok = cex_buy_walk_ask_impact_bps(book, 50.0, max_levels=5)
    assert ok is False
    assert impact >= 500.0
    assert eff == 100.0
