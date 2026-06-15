"""Backrun victim-signature dedup tests."""

from __future__ import annotations

import pytest

from src.strategies import backrun_executor as be


@pytest.mark.asyncio
async def test_backrun_dedup_skips_second_call(monkeypatch):
    monkeypatch.setenv("BACKRUN_DEDUP_TTL_SEC", "60")
    monkeypatch.setenv("TEST_MODE", "true")
    be._recent_victim_sigs.clear()
    ex = be.BackrunExecutor()
    ctx = {
        "amount_micro": 40_000_000,
        "midcap_mint": "So11111111111111111111111111111111111111112",
        "tx_sig": "dedup-test-sig",
    }
    first = await ex.execute(ctx)
    assert first is False  # test_mode
    assert be._was_recently_processed("dedup-test-sig") is True
    second = await ex.execute(ctx)
    assert second is False  # dedup skip
