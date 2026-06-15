# src/dex/executor.py — pick best DEX venue quote (Phoenix V1 vs Jupiter) for CEX→DEX sell probes.
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.config.settings import get_settings
from src.dex.jupiter import JupQuote, JupiterClient, get_jupiter_executor
from src.dex.phoenix import PhoenixExecutor, _enabled as phoenix_enabled, get_phoenix_executor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DexQuote:
    """Normalized DEX sell-leg probe: USDC per 1 SOL (or base unit for Jupiter probe)."""

    price: float
    venue: str  # "phoenix" | "jupiter"
    raw: dict[str, Any] | None = None
    out_amount: int = 0


def _jup_to_dex(q: JupQuote) -> DexQuote:
    return DexQuote(
        price=float(q.price),
        venue="jupiter",
        raw=q.raw,
        out_amount=int(q.out_amount or 0),
    )


class DexExecutor:
    """CEX-DEX venue picker: compare Phoenix on-chain ladder vs Jupiter aggregate."""

    def __init__(
        self,
        *,
        jupiter: JupiterClient | None = None,
        phoenix: PhoenixExecutor | None = None,
    ) -> None:
        self.jupiter = jupiter or get_jupiter_executor()
        self.phoenix = phoenix or get_phoenix_executor()

    def _enable_phoenix_v1(self) -> bool:
        try:
            settings = get_settings()
            if hasattr(settings, "ENABLE_PHOENIX_V1"):
                return bool(settings.ENABLE_PHOENIX_V1)
            if hasattr(settings, "enable_phoenix_v1"):
                return bool(settings.enable_phoenix_v1)
        except Exception:
            pass
        return phoenix_enabled()

    async def _phoenix_dex_quote(self, size_usdc: int) -> DexQuote | None:
        try:
            px = await self.phoenix.get_implied_usdc_per_sol(size_usdc)
        except Exception as exc:
            logger.debug("Phoenix quote skipped (RPC/venue): %s", exc)
            return None
        if px is None or px <= 0:
            return None
        return DexQuote(price=float(px), venue="phoenix")

    async def get_best_dex_price(
        self,
        size_usdc: int,
        *,
        use_phoenix: bool = True,
        jupiter_quote: JupQuote | None = None,
        jupiter_price: float | None = None,
        slippage_bps: int | None = None,
    ) -> DexQuote | None:
        """
        Return the better DEX sell reference (higher USDC per SOL wins).

        ``use_phoenix`` should be False for non-SOL base probes (Phoenix is SOL/USDC only).
        Pass ``jupiter_price`` or ``jupiter_quote`` when Jupiter was already probed.
        """
        amount = int(size_usdc)
        if amount <= 0:
            return None

        jup_dex: DexQuote | None = None
        if jupiter_quote and jupiter_quote.price > 0:
            jup_dex = _jup_to_dex(jupiter_quote)
        elif jupiter_price is not None and jupiter_price > 0:
            jup_dex = DexQuote(price=float(jupiter_price), venue="jupiter")
        else:
            kwargs: dict[str, Any] = {"amount": amount}
            if slippage_bps is not None:
                kwargs["slippage_bps"] = slippage_bps
            jup = await self.jupiter.get_quote(**kwargs)
            if jup and jup.price > 0:
                jup_dex = _jup_to_dex(jup)

        phoenix_dex: DexQuote | None = None
        if use_phoenix and self._enable_phoenix_v1():
            phoenix_dex = await self._phoenix_dex_quote(amount)

        if phoenix_dex and (not jup_dex or phoenix_dex.price > jup_dex.price):
            logger.debug(
                "DEX venue pick phoenix %.4f > jupiter %.4f (probe %s micro)",
                phoenix_dex.price,
                jup_dex.price if jup_dex else 0.0,
                amount,
            )
            return phoenix_dex
        return jup_dex


_dex_executor: DexExecutor | None = None


def get_dex_executor() -> DexExecutor:
    global _dex_executor
    if _dex_executor is None:
        _dex_executor = DexExecutor()
    return _dex_executor
