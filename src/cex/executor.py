# src/cex/executor.py
"""Thin CEX facade — all Backpack I/O via ``src.cex.backpack.BackpackClient``."""

from __future__ import annotations

import logging
from typing import Any

from src.config.settings import settings

logger = logging.getLogger(__name__)


def _bp():
    from src.cex.backpack import get_backpack_client

    return get_backpack_client()


class CexExecutor:
    def __init__(self) -> None:
        logger.info("CexExecutor initialized (delegates to BackpackClient)")

    def _market_symbol(self, symbol: str) -> str:
        return (symbol or "SOL_USDC").replace("/", "_")

    async def get_best_ask(self, symbol: str = "SOL_USDC") -> float | None:
        top = await _bp().get_best_bid_ask(self._market_symbol(symbol))
        return top.get("best_ask")

    async def get_bid_ask(
        self, symbol: str = "SOL/USDC"
    ) -> tuple[float | None, float | None]:
        top = await _bp().get_best_bid_ask(self._market_symbol(symbol))
        return top.get("best_bid"), top.get("best_ask")

    async def get_mid_price(self, symbol: str = "SOL_USDC") -> float | None:
        return await _bp().get_market_mid_price(self._market_symbol(symbol))

    async def get_cex_buy_reference_price(
        self, symbol: str = "SOL_USDC"
    ) -> tuple[float | None, float | None, float | None]:
        return await _bp().get_cex_buy_reference_price(self._market_symbol(symbol))

    async def buy_sol(
        self, size_usdc_micro: int, *, price: float | None = None
    ) -> dict[str, Any] | None:
        if size_usdc_micro <= 0:
            return None
        client = _bp()
        px = price if price and price > 0 else await client.get_best_ask("SOL_USDC")
        if not px or px <= 0:
            logger.warning("buy_sol: no CEX ask price")
            return None
        return await client.place_order("buy", float(size_usdc_micro), symbol="SOL_USDC")

    async def place_order(
        self,
        side: str,
        amount_usdc: float,
        price: float,
        symbol: str = "SOL_USDC",
    ) -> dict[str, Any] | None:
        if settings.test_mode:
            logger.info("TEST MODE: Would %s %.2f USDC @ %s", side, amount_usdc, price)
            return {"id": "test_order_123", "status": "filled"}
        if not settings.live_trading_confirm_enabled:
            logger.warning("LIVE_TRADING_CONFIRM=False - skipping CEX order")
            return None
        micro = int(amount_usdc * 1_000_000) if amount_usdc < 1_000_000 else int(amount_usdc)
        return await _bp().place_order(side, float(micro), symbol=symbol)

    async def withdraw_sol(self, amount_sol: float, destination: str) -> bool:
        result = await _bp().withdraw_sol(amount_sol, destination)
        return bool(result.get("success"))

    async def withdraw(
        self, currency: str, amount: float, address: str
    ) -> dict[str, Any] | None:
        if currency.upper() == "SOL":
            ok = await self.withdraw_sol(amount, address)
            return {"status": "ok"} if ok else None
        logger.error("Withdrawal not implemented for %s (SOL only)", currency)
        return None

    async def get_balance(self, asset: str = "USDC") -> float:
        try:
            return await _bp().get_balance(asset)
        except Exception as exc:
            logger.warning("Balance fetch failed: %s", exc)
            return 0.0

    async def check_ask_depth(
        self,
        *,
        symbol: str = "SOL",
        required_usdc: int,
        depth_mult: float | None = None,
    ) -> bool:
        return await _bp().check_ask_depth(
            symbol=symbol,
            required_usdc=required_usdc,
            depth_mult=depth_mult,
        )

    async def close(self) -> None:
        await _bp().close()


cex_executor = CexExecutor()
