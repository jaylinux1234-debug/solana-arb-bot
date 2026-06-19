"""Vol gate uses max gross across pairs (not SOL-only)."""

from __future__ import annotations

import os

import pytest

from src.strategies.volatility_gate import should_skip_low_vol_cycle


def test_skip_when_low_vol_and_low_max_gross(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEX_DEX_VOL_GATE_ENABLED", "true")
    monkeypatch.setenv("CEX_DEX_VOL_5M_LOW_THRESHOLD_PCT", "0.72")
    monkeypatch.setenv("CEX_DEX_VOL_SKIP_MAX_GROSS_BPS", "80")
    assert should_skip_low_vol_cycle(0.06, 10.0, best_pair="SOL") is True


def test_pass_when_meme_has_high_gross_despite_low_vol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEX_DEX_VOL_GATE_ENABLED", "true")
    monkeypatch.setenv("CEX_DEX_VOL_5M_LOW_THRESHOLD_PCT", "0.72")
    monkeypatch.setenv("CEX_DEX_VOL_SKIP_MAX_GROSS_BPS", "80")
    assert should_skip_low_vol_cycle(0.06, 85.0, best_pair="BONK") is False


def test_pass_when_vol_high_even_if_gross_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEX_DEX_VOL_GATE_ENABLED", "true")
    monkeypatch.setenv("CEX_DEX_VOL_5M_LOW_THRESHOLD_PCT", "0.72")
    monkeypatch.setenv("CEX_DEX_VOL_SKIP_MAX_GROSS_BPS", "80")
    assert should_skip_low_vol_cycle(1.0, 5.0) is False


def test_disabled_gate_never_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEX_DEX_VOL_GATE_ENABLED", "false")
    assert should_skip_low_vol_cycle(0.01, 0.0) is False
