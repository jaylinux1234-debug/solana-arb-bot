"""Cycle inventory snapshot integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.v2.config import V2Config
from src.v2.cycle import V2Cycle, _maybe_log_inventory_snapshot
from src.v2.dex_cex_reverse import V2ReverseLane


@pytest.mark.asyncio
async def test_cycle_logs_snapshot_on_inventory_block(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("V2_LOG_INVENTORY_SNAPSHOT", "true")

    lane = V2ReverseLane(MagicMock(), V2Config())
    lane.inventory.get_inventory_snapshot = AsyncMock(
        return_value={"backpack_sol": 0.05, "backpack_usdc": 148.0}
    )

    reverse = MagicMock()
    reverse.backpack = MagicMock()
    reverse.wallet_pubkey = "wallet"

    summary = {"cycle": 42, "block_reason": "inventory_replenish_failed"}
    await _maybe_log_inventory_snapshot(lane, reverse, summary)

    lane.inventory.get_inventory_snapshot.assert_awaited_once()
    assert "inventory_snapshot" in summary
    assert summary["inventory_snapshot"]["backpack_sol"] == 0.05


def test_v2cycle_exposes_inventory():
    reverse = MagicMock()
    reverse.backpack = MagicMock()
    reverse.wallet_pubkey = "wallet"
    lane = V2ReverseLane(reverse, V2Config())
    cycle = V2Cycle(reverse, V2Config(), lane)
    assert cycle.inventory is lane.inventory
