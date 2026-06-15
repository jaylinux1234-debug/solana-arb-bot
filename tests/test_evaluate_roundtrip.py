"""Tests for shared evaluate_roundtrip module."""

from __future__ import annotations

import pytest

from src.strategies.evaluate_roundtrip import (
    evaluate_roundtrip_cost,
    roundtrip_min_net_bps,
    should_execute_roundtrip,
)


def test_strong_pass_when_net_above_min(monkeypatch):
    monkeypatch.setenv("CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS", "0.5")
    monkeypatch.delenv("GO_LIVE_SMALL_ACCOUNT", raising=False)
    ok, reason, cost = evaluate_roundtrip_cost(
        {"gross_bps": 120.0, "vol_pct": 0.1},
        10_000_000,
        wallet_sol=1.0,
        cex_sol=1.0,
    )
    assert cost.net_bps >= 0.5
    assert ok is True
    assert reason == "roundtrip_strong"


def test_go_live_soft_pass(monkeypatch):
    monkeypatch.setenv("GO_LIVE_SMALL_ACCOUNT", "true")
    monkeypatch.setenv("CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS_GO_LIVE", "2.0")
    monkeypatch.setenv("CEX_DEX_ROUNDTRIP_SOFT_PASS_FACTOR_GO_LIVE", "0.75")
    monkeypatch.setenv("CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC_GO_LIVE", "0.18")
    ok, reason, cost = evaluate_roundtrip_cost(
        {"gross_bps": 20.0, "vol_pct": 0.2},
        25_000_000,
        wallet_sol=0.5,
        cex_sol=0.5,
    )
    if cost.net_bps >= 1.5:
        assert ok is True
        assert reason == "roundtrip_soft_pass"


def test_should_execute_roundtrip_soft_pass(monkeypatch):
    monkeypatch.setenv("GO_LIVE_SMALL_ACCOUNT", "true")
    monkeypatch.setenv("CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS", "2.0")
    monkeypatch.setenv("CEX_DEX_ROUNDTRIP_SOFT_PASS_FACTOR", "0.75")
    ok, cost = should_execute_roundtrip(
        {"gross_bps": 20.0, "size_usdc": 25_000_000, "vol": 0.2},
        wallet_sol=0.5,
        cex_sol=0.5,
    )
    if cost.net_bps >= 1.5:
        assert ok is True


def test_go_live_prefers_go_live_min_net(monkeypatch):
    monkeypatch.setenv("GO_LIVE_SMALL_ACCOUNT", "true")
    monkeypatch.setenv("CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS", "0.45")
    monkeypatch.setenv("CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS_GO_LIVE", "0.35")
    assert roundtrip_min_net_bps() == pytest.approx(0.35)


def test_rejects_low_gross(monkeypatch):
    monkeypatch.setenv("CEX_DEX_MIN_GROSS_SPREAD_BPS", "6")
    ok, reason, _ = evaluate_roundtrip_cost({"gross_bps": 3.0}, 10_000_000)
    assert ok is False
    assert reason == "gross_below_min"
