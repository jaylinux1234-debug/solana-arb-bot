"""Unified inventory manager tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.v2.config import V2Config
from src.v2.inventory_manager import InventoryManager


def test_estimate_required_cex_sol_from_trade_size():
    mgr = InventoryManager(V2Config())
    required = mgr.estimate_required_cex_sol(12_000_000, cex_bid=60.0)
    # 12 USDC / 60 bid * 0.97 reserve ≈ 0.194
    assert 0.17 < float(required) < 0.22


def test_inventory_delegates_usdc_trade_size():
    cfg = V2Config(min_trade_usdc=6.0, max_trade_usdc_micro=12_000_000)
    mgr = InventoryManager(cfg)
    micro = mgr.trade_size_micro(20.0, 12_000_000)
    assert micro == 12_000_000


def test_inventory_snapshot_fields():
    import asyncio

    class FakeBackpack:
        async def get_balance(self, asset: str, *, force_refresh: bool = False) -> float:
            return 0.35 if asset == "SOL" else 148.0

    mgr = InventoryManager(V2Config())
    snap = asyncio.run(
        mgr.get_inventory_snapshot(FakeBackpack(), wallet_pubkey=None)
    )
    assert snap["backpack_sol"] == 0.35
    assert snap["backpack_usdc"] == 148.0
    assert "timestamp" in snap
    assert snap["target_sol"] == pytest.approx(0.35, abs=0.1)


def test_ensure_cex_sol_skips_when_sufficient():
    import asyncio

    class FakeBackpack:
        async def get_balance(self, asset: str, *, force_refresh: bool = False) -> float:
            return 0.5

    mgr = InventoryManager(V2Config())
    ok = asyncio.run(
        mgr.ensure_cex_sol(Decimal("0.1"), FakeBackpack())
    )
    assert ok is True
