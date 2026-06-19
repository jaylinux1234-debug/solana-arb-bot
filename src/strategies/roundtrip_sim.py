"""CEX-buy → DEX-sell roundtrip quote simulation with structured logging."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.config.settings import Settings, get_settings
from src.dex.jupiter import SOL_MINT, USDC_MINT
from src.dex.jupiter_params import resolve_slippage_bps
from src.strategies.cex_dex_roundtrip import (
    roundtrip_net_gate_passes,
    roundtrip_sim_min_net_bps,
)

if TYPE_CHECKING:
    from src.dex.jupiter import JupiterClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DexSellQuote:
    """DEX sell-leg reference price (USDC per 1 SOL)."""

    price: float
    venue: str
    raw: dict[str, Any] | None = None


class RoundtripSimulator:
    """Model CEX ask vs best DEX sell price before live execution."""

    def __init__(
        self,
        jupiter: JupiterClient,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.jupiter = jupiter
        self.settings = settings or get_settings()

    async def get_dex_sell_quote(
        self,
        size_usdc_micro: int,
        *,
        cex_buy_price: float,
        base_mint: str = SOL_MINT,
        base_decimals: int = 9,
    ) -> DexSellQuote | None:
        """Best DEX sell reference for ``size_usdc_micro`` (Phoenix vs Jupiter sell quote)."""
        if size_usdc_micro <= 0 or cex_buy_price <= 0:
            return None

        if base_mint == SOL_MINT:
            from src.dex.executor import get_dex_executor

            sell_px, _ = await self.jupiter.get_implied_usdc_per_base_sell(
                int(size_usdc_micro),
                SOL_MINT,
                cex_buy_price,
                base_decimals=9,
            )
            picked = await get_dex_executor().get_best_dex_price(
                int(size_usdc_micro),
                use_phoenix=True,
                jupiter_price=sell_px,
            )
            if picked and picked.price > 0:
                return DexSellQuote(
                    price=float(picked.price),
                    venue=picked.venue,
                    raw=picked.raw,
                )

        slippage_bps = resolve_slippage_bps(base_mint, USDC_MINT)
        px, quote = await self.jupiter.get_implied_usdc_per_base_sell(
            int(size_usdc_micro),
            base_mint,
            cex_buy_price,
            base_decimals=base_decimals,
            slippage_bps=slippage_bps,
        )
        if px and px > 0:
            return DexSellQuote(
                price=float(px),
                venue="jupiter_sell",
                raw=quote if isinstance(quote, dict) else None,
            )
        return None

    async def run_roundtrip(
        self,
        cex_buy_price: float,
        size_usdc_micro: int,
        *,
        base_mint: str = SOL_MINT,
        base_decimals: int = 9,
        expected_net_bps: float | None = None,
    ) -> tuple[bool, float, str, dict[str, Any]]:
        """
        Price-based gate plus full Jupiter sell quote simulation.

        Returns ``(ok, modeled_net_bps, reason, details)``.
        """
        if cex_buy_price <= 0 or size_usdc_micro <= 0:
            return False, 0.0, "invalid_inputs", {}

        dex_sell = await self.get_dex_sell_quote(
            size_usdc_micro,
            cex_buy_price=cex_buy_price,
            base_mint=base_mint,
            base_decimals=base_decimals,
        )
        if dex_sell is None or dex_sell.price <= 0:
            return False, 0.0, "dex_sell_quote_failed", {}

        usdc = size_usdc_micro / 1_000_000.0
        eff_cex = cex_buy_price
        depth_meta: dict[str, Any] = {}
        if os.getenv("CEX_DEX_ROUNDTRIP_USE_DEPTH", "true").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            try:
                from src.cex.backpack import get_backpack_client
                from src.cex.backpack_ticker import cex_buy_walk_ask_impact_bps

                levels = int(os.getenv("CEX_DEX_ROUNDTRIP_DEPTH_LEVELS", "5"))
                book = await get_backpack_client().get_orderbook("SOL_USDC", limit=levels)
                impact_bps, eff_price, ok = cex_buy_walk_ask_impact_bps(
                    book, usdc, max_levels=levels
                )
                depth_meta = {
                    "cex_depth_impact_bps": round(impact_bps, 2),
                    "cex_effective_ask": round(eff_price, 6),
                }
                if ok:
                    eff_cex = eff_price
            except Exception as exc:
                logger.debug("roundtrip_sim depth skipped: %s", exc)

        modeled_net = (dex_sell.price - eff_cex) / eff_cex * 10_000.0
        min_net = roundtrip_sim_min_net_bps()
        details: dict[str, Any] = {
            "cex_buy_price": cex_buy_price,
            "cex_effective_buy_price": round(eff_cex, 6),
            "dex_sell_price": dex_sell.price,
            "dex_venue": dex_sell.venue,
            "size_usdc_micro": size_usdc_micro,
            "modeled_net_bps": round(modeled_net, 2),
            "min_net_bps": min_net,
            **depth_meta,
        }

        logger.info(
            "roundtrip_sim",
            extra={
                "modeled_net_bps": round(modeled_net, 2),
                "dex_venue": dex_sell.venue,
                "size_usdc": size_usdc_micro,
                "cex_buy_price": cex_buy_price,
                "dex_sell_price": dex_sell.price,
            },
        )

        from src.strategies.cex_dex_roundtrip import pre_simulate_cex_buy_dex_sell

        probe_micro = int(
            os.getenv(
                "CEX_DEX_PROBE_USDC_MICRO",
                str(getattr(self.settings, "CEX_DEX_PROBE_USDC_MICRO", 0) or 0),
            )
        )
        sim_ok, sim_net, sim_reason, sim_details = await pre_simulate_cex_buy_dex_sell(
            self.jupiter,
            size_usdc_micro,
            cex_buy_price,
            backpack_symbol=str(opportunity.get("backpack_symbol") or "SOL_USDC"),
            base_mint=base_mint,
            base_decimals=base_decimals,
            expected_net_bps=expected_net_bps,
            probe_usdc_micro=probe_micro,
        )
        details.update(sim_details)
        details["price_modeled_net_bps"] = round(modeled_net, 2)
        details["quote_modeled_net_bps"] = round(sim_net, 2)

        if not sim_ok:
            return False, sim_net, sim_reason, details

        price_ok, price_near_miss = roundtrip_net_gate_passes(modeled_net, min_net)
        details["price_roundtrip_near_miss_ok"] = price_near_miss
        if not price_ok:
            return False, modeled_net, f"price_net_below_{min_net:.1f}bps", details

        reason = "near_miss_ok" if details.get("roundtrip_near_miss_ok") else "ok"
        return True, sim_net, reason, details
