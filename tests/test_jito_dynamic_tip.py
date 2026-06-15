"""Dynamic Jito tip sizing (profit ratio + fill rate)."""

from __future__ import annotations

import pytest

from src.core import jito_tip


def test_compute_dynamic_tip_profit_ratio(monkeypatch):
    monkeypatch.setenv("JITO_TIP_MIN_LAMPORTS", "80000")
    monkeypatch.setenv("JITO_TIP_MAX_LAMPORTS", "250000")
    monkeypatch.setenv("JITO_TIP_PROFIT_RATIO", "0.35")

    # 1M lamports profit → 350k capped to 250k
    assert jito_tip.compute_dynamic_jito_tip_lamports(1_000_000) == 250_000
    # 200k profit → 70k, floored to 80k base
    assert jito_tip.compute_dynamic_jito_tip_lamports(200_000) == 80_000
    # zero profit → base only
    assert jito_tip.compute_dynamic_jito_tip_lamports(0) == 80_000


def test_fill_rate_boost_low_rate():
    mult = jito_tip._jito_fill_rate_boost(0.0)
    assert mult > 1.0


def test_fill_rate_boost_high_rate():
    mult = jito_tip._jito_fill_rate_boost(0.95)
    assert mult < 1.0


def test_expected_profit_lamports(monkeypatch):
    monkeypatch.setenv("JITO_TIP_SOL_USD", "100")
    # 10 bps on $50 = $0.05 → 0.0005 SOL → 500_000 lamports
    lam = jito_tip.expected_profit_lamports(10.0, 50_000_000)
    assert lam == 500_000


@pytest.mark.asyncio
async def test_get_dynamic_jito_tip(monkeypatch):
    monkeypatch.setenv("JITO_TIP_MIN_LAMPORTS", "80000")
    monkeypatch.setenv("JITO_TIP_MAX_LAMPORTS", "250000")
    monkeypatch.setenv("JITO_TIP_PROFIT_RATIO", "0.35")
    monkeypatch.setenv("JITO_TIP_USE_LIVE_FLOOR", "false")

    jito_tip._JITO_OUTCOMES.clear()
    monkeypatch.setattr(jito_tip, "jito_recent_bundle_fill_rate", lambda **_: 0.7)
    tip = await jito_tip.get_dynamic_jito_tip(300_000)
    assert 80_000 <= tip <= 250_000
    assert tip == 105_000
