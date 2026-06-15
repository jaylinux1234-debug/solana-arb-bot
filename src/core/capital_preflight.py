"""Capital preflight before CEX-DEX live trades (Backpack USDC + on-chain SOL for fees)."""

from __future__ import annotations

import logging
import os

from src.cex.backpack import BackpackClient, get_backpack_client
from src.core.wallet import get_wallet_pubkey

logger = logging.getLogger(__name__)


class InsufficientBalance(Exception):
    """Raised when Backpack USDC or on-chain SOL is too low for the planned trade."""

    def __init__(self, message: str, *, asset: str = "") -> None:
        super().__init__(message)
        self.asset = asset


async def get_wallet_sol_balance() -> float:
    """Native SOL balance for ``WALLET_PUBKEY`` (fee reserve)."""
    from src.core.wallet import get_sol_balance_robust

    return await get_sol_balance_robust()


async def get_ledger_sol_balance() -> float:
    """Backward-compatible alias for ``get_wallet_sol_balance``."""
    return await get_wallet_sol_balance()


async def preflight_check(
    size_usdc_micro: int,
    *,
    backpack: BackpackClient | None = None,
    usdc_buffer_mult: float = 1.05,
    min_wallet_sol: float | None = None,
) -> None:
    """
    Verify Backpack USDC and on-chain SOL before a live CEX-DEX trade.

    ``size_usdc_micro`` is USDC in 6-decimal micro-units.
    """
    if size_usdc_micro <= 0:
        raise InsufficientBalance("invalid trade size", asset="USDC")

    bp = backpack or get_backpack_client()
    backpack_usdc = await bp.get_balance("USDC", force_refresh=True)
    required_usdc = float(size_usdc_micro) / 1_000_000.0
    need_usdc = required_usdc * float(usdc_buffer_mult)

    if backpack_usdc < need_usdc:
        raise InsufficientBalance(
            f"Backpack USDC: have ${backpack_usdc:.2f}, need ${need_usdc:.2f} "
            f"(trade ${required_usdc:.2f} × {usdc_buffer_mult})",
            asset="USDC",
        )

    floor_sol = min_wallet_sol
    if floor_sol is None:
        try:
            floor_sol = float(
                os.getenv("CEX_DEX_MIN_WALLET_SOL")
                or os.getenv("CEX_DEX_MIN_LEDGER_SOL", "0.12")
            )
        except (TypeError, ValueError):
            floor_sol = 0.12

    wallet_sol = await get_wallet_sol_balance()
    if wallet_sol < floor_sol:
        raise InsufficientBalance(
            f"Wallet SOL for fees: have {wallet_sol:.4f}, need {floor_sol:.4f}",
            asset="SOL",
        )

    logger.debug(
        "capital preflight ok | backpack_usdc=%.2f need=%.2f wallet_sol=%.4f",
        backpack_usdc,
        need_usdc,
        wallet_sol,
    )
