"""Tests for src.utils.signals backrun TTL cache."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.strategies.brain_signals import backrun_signal_present, brain_snapshot, reset_cycle_signals
from src.utils.signals import get_backrun_context, set_backrun_ttl


@pytest.mark.asyncio
async def test_utils_signals_ttl_survives_reset(monkeypatch):
    monkeypatch.setenv("BACKRUN_SIGNAL_TTL_SEC", "30")
    monkeypatch.setenv("ENABLE_HELIUS_WEBHOOK_BACKRUN", "true")
    monkeypatch.setenv("HELIUS_BACKRUN_MIN_AMOUNT_MICRO", "50000000")
    set_backrun_ttl({"active": True, "amount_micro": 60_000_000})
    await asyncio.sleep(0)
    reset_cycle_signals()
    assert get_backrun_context().get("active") is True
    assert backrun_signal_present(brain_snapshot()) is True


def test_utils_signals_expires(monkeypatch):
    monkeypatch.setenv("BACKRUN_SIGNAL_TTL_SEC", "0.01")
    set_backrun_ttl({"active": True, "amount_micro": 60_000_000})
    time.sleep(0.02)
    assert get_backrun_context().get("active") is not True
