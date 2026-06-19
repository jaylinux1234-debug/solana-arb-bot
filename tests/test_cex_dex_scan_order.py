"""CEX-DEX scan ordering: vol-gate priority + liquid focus pairs."""

from __future__ import annotations

import pytest

from src.cex.trading_pairs import CexDexPair
from src.strategies.cex_dex_strategy import CexDexStrategy


def _pair(symbol: str) -> CexDexPair:
    return CexDexPair(
        symbol=symbol,
        base_mint=f"mint_{symbol}",
        base_decimals=9,
        backpack_symbol=f"{symbol}_USDC",
        pair_label=f"{symbol}/USDC",
    )


@pytest.fixture
def strategy() -> CexDexStrategy:
    s = object.__new__(CexDexStrategy)
    s._pairs = [
        _pair("SOL"),
        _pair("BONK"),
        _pair("WIF"),
        _pair("POPCAT"),
        _pair("MEW"),
        _pair("PNUT"),
    ]
    return s


def test_pairs_for_scan_prioritizes_vol_gate_best(strategy: CexDexStrategy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEX_DEX_FOCUS_SCAN_SYMBOLS", "all")
    ordered = strategy._pairs_for_scan("WIF")
    assert [p.symbol for p in ordered] == ["WIF", "SOL", "BONK", "POPCAT"]


def test_pairs_for_scan_liquid_focus_default(strategy: CexDexStrategy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CEX_DEX_FOCUS_SCAN_SYMBOLS", raising=False)
    ordered = strategy._pairs_for_scan("BONK")
    assert [p.symbol for p in ordered] == ["BONK", "SOL", "WIF", "POPCAT", "MEW", "PNUT"]


def test_focus_scan_symbols_all_disables_filter(strategy: CexDexStrategy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEX_DEX_FOCUS_SCAN_SYMBOLS", "all")
    assert strategy._focus_scan_symbols() is None
    assert len(strategy._pairs_for_scan(None)) == 4
