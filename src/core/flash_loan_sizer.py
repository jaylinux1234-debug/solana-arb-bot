# src/core/flash_loan_sizer.py
from __future__ import annotations

import logging
from decimal import Decimal

from src.config.settings import settings
from src.dex.kamino import get_kamino_reserve_liquidity  # assume this exists

logger = logging.getLogger(__name__)

class FlashLoanSizer:
    """Dynamic sizing engine for 30k-500k USDC sweet spot"""

    MIN_USDC = Decimal("30000")
    MAX_USDC = Decimal("500000")
    STEP = Decimal("50000")

    @staticmethod
    async def get_optimal_size(
        opportunity_type: str = "cex_dex",
        current_sol_price: Decimal | None = None,
        available_liquidity_usdc: Decimal | None = None,
        expected_gross_bps: int = 0
    ) -> Decimal:
        """
        Returns optimal flash loan size in USDC (micro units)
        """
        if current_sol_price is None:
            current_sol_price = await get_current_sol_price()  # implement or use cache

        # Base size by strategy
        if opportunity_type == "cex_dex":
            base_size = Decimal(settings.CEX_DEX_FLASH_AMOUNT_USDC_MICRO) / 1_000_000
        else:
            base_size = Decimal(settings.COLLATERAL_FLASH_AMOUNT_USDC_MICRO) / 1_000_000

        # Get real liquidity
        if available_liquidity_usdc is None:
            available_liquidity_usdc = await get_kamino_reserve_liquidity(
                settings.KAMINO_LENDING_MARKET_PUBKEY
            )

        # Dynamic calculation
        target = min(
            base_size,
            available_liquidity_usdc * Decimal(settings.FLASH_SIZE_UTILIZATION)
        )

        # Respect min/max
        target = max(FlashLoanSizer.MIN_USDC, min(FlashLoanSizer.MAX_USDC, target))

        # Price impact safety
        if expected_gross_bps > 0:
            impact_adjusted = target * (
                Decimal("1") - (Decimal(expected_gross_bps) / Decimal("10000")) * Decimal("0.3")
            )
            target = min(target, impact_adjusted)

        # Round to nice step
        optimal = (target // FlashLoanSizer.STEP) * FlashLoanSizer.STEP
        optimal = max(FlashLoanSizer.MIN_USDC, optimal)

        logger.info(
            f"Flash size decision | type={opportunity_type} | "
            f"optimal={optimal} USDC | liquidity={available_liquidity_usdc:.0f}"
        )

        return optimal * 1_000_000  # return micro units

async def get_current_sol_price() -> Decimal:
    """Use CEX cache or Jupiter quote"""
    # Implement using your existing CEX price feed
    from src.cex.price_feed import get_price

    price = await get_price("SOL/USDC")
    if price is None:
        return Decimal("0")
    return Decimal(str(price))