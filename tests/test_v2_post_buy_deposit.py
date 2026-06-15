"""Post-buy Backpack SOL sync and settle-trust behavior."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from src.v2.config import V2Config
from src.v2.inventory_manager import InventoryManager


@pytest.mark.asyncio
async def test_ensure_cex_sol_trusts_onchain_deposit_when_settle_lags(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("V2_CEX_SOL_TRUST_ONCHAIN_DEPOSIT", "true")
    monkeypatch.setenv("V2_AUTO_DEPOSIT_SOL_TO_CEX", "true")
    monkeypatch.setenv("V2_CEX_SOL_DEPOSIT_SETTLE_SEC", "1")

    class FakeBackpack:
        balances = [0.05, 0.05, 0.05]

        async def get_balance(self, asset: str, *, force_refresh: bool = False) -> float:
            return self.balances[min(len(self.balances) - 1, 0)]

        async def get_deposit_address(self, chain: str) -> dict:
            return {"success": True, "address": "Dep111111111111111111111111111111111"}

        def clear_balance_cache(self, asset: str) -> None:
            pass

    backpack = FakeBackpack()
    mgr = InventoryManager(V2Config(), backpack=backpack)

    with patch.object(
        mgr,
        "deposit_wallet_sol_to_backpack",
        new=AsyncMock(
            return_value={
                "success": True,
                "tx_sig": "5abcDepTx",
                "backpack_sol_after": 0.05,
            }
        ),
    ):
        ok = await mgr.ensure_cex_sol(Decimal("0.12"), backpack)

    assert ok is True
