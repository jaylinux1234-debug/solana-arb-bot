"""MEV dispatch routing tests."""

from __future__ import annotations

import pytest

from src.strategies.mev_dispatch import execute_mev_lane


@pytest.mark.asyncio
async def test_backrun_dispatch_idle_without_signal(monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    ok = await execute_mev_lane("backrun", {"backrun": {"active": False}})
    assert ok is False


@pytest.mark.asyncio
async def test_unknown_lane_returns_false():
    assert await execute_mev_lane("unknown_lane", {}) is False
