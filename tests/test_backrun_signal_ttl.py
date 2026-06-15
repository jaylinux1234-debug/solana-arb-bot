"""Backrun webhook signal TTL tests."""

from __future__ import annotations

import time

import pytest

from src.strategies.brain_signals import (
    backrun_signal_present,
    backrun_signal_still_valid,
    brain_snapshot,
    get_backrun_context,
    note_backrun_context,
    reset_cycle_signals,
    set_backrun_ttl,
)


def test_backrun_ttl_survives_reset_cycle_signals(monkeypatch):
    monkeypatch.setenv("BACKRUN_SIGNAL_TTL_SEC", "30")
    monkeypatch.setenv("ENABLE_HELIUS_WEBHOOK_BACKRUN", "true")
    monkeypatch.setenv("HELIUS_BACKRUN_MIN_AMOUNT_MICRO", "50000000")
    set_backrun_ttl({"active": True, "amount_micro": 60_000_000})
    reset_cycle_signals()
    snap = brain_snapshot()
    assert backrun_signal_present(snap) is True


def test_backrun_expires_after_ttl(monkeypatch):
    monkeypatch.setenv("BACKRUN_SIGNAL_TTL_SEC", "0.01")
    set_backrun_ttl({"active": True, "amount_micro": 60_000_000})
    time.sleep(0.02)
    assert backrun_signal_still_valid(get_backrun_context()) is False
    assert backrun_signal_present(brain_snapshot()) is False


def test_note_backrun_context_sets_ttl(monkeypatch):
    monkeypatch.setenv("BACKRUN_SIGNAL_TTL_SEC", "30")
    note_backrun_context({"active": True, "amount_micro": 55_000_000})
    ctx = get_backrun_context()
    assert ctx.get("active") is True
    assert ctx.get("ttl_until") is not None
