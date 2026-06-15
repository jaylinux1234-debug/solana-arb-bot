"""Backpack USDC→SOL swap fallback (gated replenish)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.v2.config import V2Config
from src.v2.inventory_manager import InventoryManager


@pytest.mark.asyncio
async def test_swap_blocked_without_strong_or_post_buy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("V2_BACKPACK_SWAP_SOL_ENABLED", "true")
    monkeypatch.setenv("V2_BACKPACK_SOL_SWAP_TRIGGER", "0.08")
    mgr = InventoryManager(V2Config())
    assert mgr._should_try_backpack_swap_replenish(
        Decimal("0.05"),
        strong_signal=False,
        after_jupiter_buy=False,
    ) is False


@pytest.mark.asyncio
async def test_swap_allowed_on_strong_when_sol_below_trigger(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("V2_BACKPACK_SWAP_SOL_ENABLED", "true")
    monkeypatch.setenv("V2_BACKPACK_SOL_SWAP_TRIGGER", "0.08")
    mgr = InventoryManager(V2Config())
    assert mgr._should_try_backpack_swap_replenish(
        Decimal("0.05"),
        strong_signal=True,
        after_jupiter_buy=False,
    ) is True


@pytest.mark.asyncio
async def test_swap_blocked_when_sol_above_trigger(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("V2_BACKPACK_SWAP_SOL_ENABLED", "true")
    monkeypatch.setenv("V2_BACKPACK_SOL_SWAP_TRIGGER", "0.08")
    mgr = InventoryManager(V2Config())
    assert mgr._should_try_backpack_swap_replenish(
        Decimal("0.10"),
        strong_signal=True,
        after_jupiter_buy=True,
    ) is False


@pytest.mark.asyncio
async def test_swap_usdc_to_sol_caps_size(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("V2_BACKPACK_SWAP_MAX_USDC", "12")
    monkeypatch.setenv("V2_BACKPACK_SWAP_USDC_RESERVE", "50")
    monkeypatch.setenv("V2_BACKPACK_SWAP_MIN_USDC", "8")

    class FakeBackpack:
        async def get_balance(self, asset: str, *, force_refresh: bool = False) -> float:
            return 148.0 if asset == "USDC" else 0.04

        async def get_best_ask(self, symbol: str) -> float:
            return 150.0

        async def place_market_buy(self, symbol: str, size_usdc_micro: int) -> dict:
            assert size_usdc_micro == 12_000_000
            return {"success": True, "orderId": "ord-1"}

        def clear_balance_cache(self, asset: str) -> None:
            pass

    mgr = InventoryManager(V2Config())
    mgr.get_backpack_sol = AsyncMock(return_value=0.12)  # type: ignore[method-assign]

    result = await mgr._swap_usdc_to_sol_on_backpack(
        FakeBackpack(),
        Decimal("0.08"),
        cex_bid=150.0,
    )
    assert result["success"] is True
    assert result["usdc_spent"] == pytest.approx(12.0)
