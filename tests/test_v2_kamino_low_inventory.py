"""Kamino flash routing when inventory is low."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.v2.config import V2Config
from src.v2.dex_cex_reverse import V2ReverseLane


def _lane(
    *,
    enable_kamino: bool = True,
    prefer_flash: bool = False,
    low_inv_flash: bool = True,
    wallet_first: bool = True,
) -> V2ReverseLane:
    cfg = V2Config(
        enable_kamino_flash=enable_kamino,
        kamino_prefer_flash=prefer_flash,
        kamino_flash_on_low_inventory=low_inv_flash,
        kamino_wallet_first=wallet_first,
        max_trade_usdc=25.0,
        max_trade_usdc_micro=25_000_000,
        kamino_flash_amount_usdc_micro=25_000_000,
        min_usdc_balance=8.0,
        min_trade_usdc=8.0,
    )
    reverse = MagicMock()
    reverse.backpack = MagicMock()
    reverse.wallet_pubkey = "wallet"
    reverse.jupiter = MagicMock()
    lane = V2ReverseLane(reverse, cfg)
    lane.usdc_manager.replenish_usdc_for_trade = AsyncMock(return_value=(5.0, ""))
    lane.usdc_manager.has_minimum = lambda avail: avail >= cfg.min_trade_usdc
    lane.usdc_manager.trade_size_micro = lambda avail, sig: min(
        int(avail * 1_000_000), sig
    )
    lane.inventory.is_inventory_healthy = AsyncMock(return_value=False)
    lane.inventory.ensure_cex_sol = AsyncMock(return_value=True)
    lane.inventory.get_backpack_sol = AsyncMock(return_value=0.3)
    return lane


@pytest.mark.asyncio
async def test_kamino_flash_when_low_usdc():
    lane = _lane()
    opp = {"size_usdc_micro": 12_000_000, "cex_bid": 150.0}
    sized, reason, _ = await lane.prepare_execution(opp)
    assert reason == ""
    assert sized is not None
    assert sized["execution_path"] == "kamino_flash"
    assert sized["size_usdc_micro"] == 12_000_000


@pytest.mark.asyncio
async def test_wallet_first_when_funded_despite_low_inv_flag():
    lane = _lane()
    lane.usdc_manager.replenish_usdc_for_trade = AsyncMock(return_value=(20.0, ""))
    opp = {"size_usdc_micro": 12_000_000, "cex_bid": 150.0}
    sized, reason, _ = await lane.prepare_execution(opp)
    assert reason == ""
    assert sized is not None
    assert sized["execution_path"] == "wallet_usdc"


@pytest.mark.asyncio
async def test_kamino_disabled_when_low_inv_flag_off():
    lane = _lane(low_inv_flash=False)
    opp = {"size_usdc_micro": 12_000_000, "cex_bid": 150.0}
    sized, reason, _ = await lane.prepare_execution(opp)
    assert sized is None
    assert reason != ""


@pytest.mark.asyncio
async def test_execute_routes_kamino_without_prefer_flash():
    lane = _lane()
    lane.execute_with_kamino_flash = AsyncMock(return_value={"status": "ok"})
    result = await lane.execute({"execution_path": "kamino_flash"})
    lane.execute_with_kamino_flash.assert_awaited_once()
    assert result["status"] == "ok"
