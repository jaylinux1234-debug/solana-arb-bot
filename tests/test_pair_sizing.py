from __future__ import annotations

import pytest

from src.core.sizing import calculate_trade_size, get_max_trade_size_micro, get_max_trade_size_usdc


def test_get_max_trade_size_micro_known_pair() -> None:
    assert get_max_trade_size_micro("BONK") == 42_000_000


def test_get_max_trade_size_usdc() -> None:
    assert get_max_trade_size_usdc("MEW") == pytest.approx(28.0)


def test_calculate_trade_size_weak_edge_scales_down() -> None:
    # BONK max $42, gross 8 bps -> 65% scale
    assert calculate_trade_size("BONK", 8.0, global_max_usdc=42.0) == int(42_000_000 * 0.65)


def test_calculate_trade_size_strong_edge_full_size() -> None:
    assert calculate_trade_size("BONK", 20.0, global_max_usdc=42.0) == 42_000_000


def test_calculate_trade_size_global_cap() -> None:
    assert calculate_trade_size("BONK", 20.0, global_max_usdc=35.0) == 35_000_000


def test_get_max_trade_size_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAIR_MAX_SIZE_BONK", "50")
    assert get_max_trade_size_usdc("BONK") == pytest.approx(50.0)


def test_get_max_trade_size_env_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAIR_MAX_SIZE_BONK", "not-a-number")
    assert get_max_trade_size_usdc("BONK") == pytest.approx(42.0)
