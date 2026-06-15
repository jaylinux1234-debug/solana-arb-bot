"""
DEX → CEX reverse arbitrage: buy SOL on Jupiter, sell on Backpack.

Profitable when DEX is cheap vs CEX (``dex_cheap``): CEX bid > Jupiter implied buy.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from src.cex.backpack import BackpackClient, BackpackExecutor
from src.config.env_access import settings_int
from src.config.settings import Settings, get_settings
from src.core.circuit_breaker import circuit_breaker
from src.core.risk import RiskEngine
from src.core.wallet import check_global_safety
from src.dex.jupiter import SOL_MINT, USDC_MINT, JupiterClient, get_jupiter_executor
from src.dex.jupiter_params import resolve_slippage_bps
from src.monitoring.metrics import (
    record_cex_dex_near_miss,
    record_trade_execution,
)
from src.strategies.cex_dex_core import (
    analyze_cex_dex_spread,
    net_spread_bps_after_costs,
)
from src.utils.price import bps_diff

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class DexCexReverseStrategy:
    """Buy cheap on DEX (Jupiter) → sell on Backpack CEX."""

    def __init__(
        self,
        jupiter_executor: JupiterClient | None = None,
        backpack_client: BackpackClient | None = None,
        wallet_pubkey: str | None = None,
        *,
        settings: Settings | None = None,
        risk: RiskEngine | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.jupiter = jupiter_executor or get_jupiter_executor()
        self.jupiter_executor = self.jupiter
        self.backpack = backpack_client or BackpackClient(self.settings)
        self.backpack_client = self.backpack
        self.wallet_pubkey = (
            wallet_pubkey
            or self.settings.wallet_pubkey
            or self.settings.WALLET_PUBKEY
            or os.getenv("WALLET_PUBKEY", "")
        )
        self.risk = risk or RiskEngine(self.settings)
        self._probe_micro = settings_int(
            self.settings,
            "CEX_DEX_PROBE_USDC_MICRO",
            default=12_000_000,
        )
        if os.getenv("CEX_DEX_PROBE_USDC_MICRO"):
            self._probe_micro = int(os.getenv("CEX_DEX_PROBE_USDC_MICRO", str(self._probe_micro)))

    async def get_bid_price(self, symbol: str = "SOL") -> float | None:
        """Backpack best bid (USDC per SOL) for CEX sell leg."""
        return await self.backpack.get_bid_price(symbol)

    async def get_jupiter_sol_price(
        self,
        size_usdc_micro: int | None = None,
        *,
        cex_reference: float | None = None,
        slippage_bps: int | None = None,
    ) -> float | None:
        """Jupiter implied USDC per SOL for ``size_usdc_micro`` probe."""
        micro = int(size_usdc_micro or self._probe_micro)
        price, _ = await self.jupiter.get_implied_usdc_per_base(
            micro,
            SOL_MINT,
            base_decimals=9,
            cex_reference=cex_reference,
            slippage_bps=slippage_bps,
        )
        return float(price) if price and price > 0 else None

    async def _detect_dex_cheap(self) -> dict[str, Any] | None:
        """Scan for Jupiter cheaper than Backpack bid (dex_cheap / reverse arb)."""
        if not _env_bool("ENABLE_DEX_CEX_REVERSE", True):
            return None

        cex_bid = await self.get_bid_price("SOL")
        jup_price = await self.get_jupiter_sol_price()
        if not cex_bid or not jup_price or cex_bid <= 0 or jup_price <= 0:
            return None

        spread = analyze_cex_dex_spread(float(cex_bid), float(jup_price))
        if spread is None or spread.direction != "dex_cheap":
            return None

        gross_bps = float(bps_diff(float(jup_price), float(cex_bid)))
        if gross_bps <= 0:
            gross_bps = spread.spread_bps_abs

        max_micro = settings_int(
            self.settings,
            "CEX_DEX_MAX_TRADE_USDC_MICRO",
            default=12_000_000,
        )
        size_micro = int(
            os.getenv("DEX_CEX_REVERSE_SIZE_USDC_MICRO", str(max_micro))
        )
        size_micro = min(size_micro, max_micro)
        net_bps = net_spread_bps_after_costs(
            gross_bps,
            size_micro,
            direction="dex_cheap",
        )

        return {
            "symbol": "SOL",
            "pair_label": "SOL/USDC",
            "direction": "dex_cheap",
            "is_dex_cheap": True,
            "gross_bps": gross_bps,
            "net_bps": net_bps,
            "size_usdc_micro": size_micro,
            "size_usdc": size_micro / 1_000_000.0,
            "cex_bid": float(cex_bid),
            "jup_price": float(jup_price),
            "path": "dex_cex_reverse",
        }

    async def scan_dex_cheap(self) -> dict[str, Any] | None:
        """Alias used by brain snapshot / multi-strategy cycle."""
        return await self._detect_dex_cheap()

    async def scan_and_execute(self, brain_score: float = 0) -> dict[str, Any]:
        """Main entry from unified cycle brain."""
        opportunity = await self._detect_dex_cheap()
        if not opportunity:
            return {"status": "no_signal"}
        return await self.execute_opportunity(opportunity, brain_score=brain_score)

    async def execute_opportunity(
        self,
        opportunity: dict[str, Any],
        *,
        brain_score: float = 0,
        slippage_bps: int | None = None,
    ) -> dict[str, Any]:
        """Jupiter USDC→SOL buy then Backpack SOL sell (v2 / brain path)."""
        if not check_global_safety() or circuit_breaker.should_pause():
            return {"status": "safety_blocked"}
        if not self.risk.can_trade(0):
            return {"status": "risk_blocked"}

        min_gross = float(os.getenv("DEX_CEX_REVERSE_MIN_GROSS_BPS", "8"))
        if brain_score >= 70:
            min_gross = max(6.0, min_gross - 2.0)

        gross_bps = float(opportunity.get("gross_bps") or 0)
        if gross_bps < min_gross:
            record_cex_dex_near_miss(gross_bps, reason="dex_reverse_env_thresholds")
            return {"status": "env_thresholds", "gross": gross_bps}

        size_micro = int(opportunity.get("size_usdc_micro") or 0)
        if size_micro <= 0:
            return {"status": "invalid_size"}
        if not self.risk.can_trade(size_micro):
            return {"status": "risk_size_blocked"}

        if not self.settings.test_mode and not self.settings.simulate:
            if not self.settings.live_trading_confirm_enabled:
                return {"status": "live_confirm_off"}
            if not await self.jupiter.has_signing():
                return {"status": "signing_unavailable"}

        logger.info(
            "DEX_CEX_REVERSE | gross=%.1fbps net=%.1fbps size=$%.2f brain=%.0f",
            gross_bps,
            float(opportunity.get("net_bps") or 0),
            size_micro / 1e6,
            brain_score,
        )

        # === INVENTORY MANAGEMENT (before Jupiter buy) ===
        if _env_bool("ENABLE_BACKPACK_AUTO_REPLENISH", True):
            from decimal import Decimal

            from src.v2.config import V2Config
            from src.v2.inventory_manager import InventoryManager

            inv = InventoryManager(V2Config.from_env(), backpack=self.backpack)
            required_sol = inv.estimate_required_cex_sol(
                size_micro,
                cex_bid=float(opportunity.get("cex_bid") or 0) or None,
            )
            inv_ok = await inv.ensure_cex_sol(
                required_sol,
                self.backpack,
                wallet_pubkey=self.wallet_pubkey,
                strong_signal=bool(
                    opportunity.get("strong_signal")
                    or opportunity.get("roundtrip_passed")
                ),
                cex_bid=float(opportunity.get("cex_bid") or 0) or None,
            )
            if not inv_ok:
                logger.error("Inventory replenish failed - skipping trade")
                return {
                    "status": "inventory_replenish_failed",
                    "live_fill": False,
                    "block_reason": "inventory_replenish_failed",
                    "jupiter_step": "preflight_inventory",
                    "required_cex_sol": float(required_sol),
                }

        slip = (
            int(slippage_bps)
            if slippage_bps is not None
            else int(
                os.getenv(
                    "DEX_CEX_REVERSE_SLIPPAGE_BPS",
                    str(resolve_slippage_bps(USDC_MINT, SOL_MINT)),
                )
            )
        )
        buy_result = await self.jupiter.execute_buy_sol(
            amount_usdc=size_micro,
            slippage_bps=slip,
            net_bps=float(opportunity.get("net_bps") or 0),
        )
        if not buy_result.get("success"):
            record_trade_execution("dex_cex_reverse", success=False, pnl_usd=0.0)
            return {"status": "jupiter_buy_failed", "error": buy_result.get("error")}

        return await self.complete_cex_sell_after_buy(
            opportunity,
            size_usdc_micro=size_micro,
            buy_result=buy_result,
            gross_bps=gross_bps,
        )

    async def complete_cex_sell_after_buy(
        self,
        opportunity: dict[str, Any],
        *,
        size_usdc_micro: int,
        buy_result: dict[str, Any],
        gross_bps: float | None = None,
    ) -> dict[str, Any]:
        """Backpack sell leg after Jupiter buy — robust confirm, size, sanitize, sell."""
        gross_bps = float(
            gross_bps if gross_bps is not None else opportunity.get("gross_bps") or 0
        )
        sol_received = float(buy_result.get("sol_received") or 0)
        if sol_received <= 0:
            lamports = int(buy_result.get("amount_lamports") or 0)
            sol_received = lamports / 1_000_000_000.0
        if sol_received <= 0:
            return {"status": "jupiter_buy_failed", "error": "zero_sol_out"}

        cex_bid_hint = float(opportunity.get("cex_bid") or 0)
        bp_market = (
            os.getenv("CEX_DEX_BACKPACK_MARKET")
            or getattr(self.settings, "CEX_DEX_BACKPACK_MARKET", None)
            or "SOL_USDC"
        )

        try:
            buffer_sec = float(os.getenv("DEX_CEX_REVERSE_CEX_BUFFER_SEC", "4"))
            await asyncio.sleep(buffer_sec)

            from src.core.rpc_config import get_robust_sol_balance

            chain_sol = float(await get_robust_sol_balance(self.wallet_pubkey))
            sell_reserve_pct = float(
                os.getenv("DEX_CEX_REVERSE_SELL_RESERVE_PCT", "0.03")
            )
            sell_reserve = float(os.getenv("DEX_CEX_REVERSE_SELL_RESERVE_SOL", "0.01"))
            cex_reserve = float(os.getenv("DEX_CEX_REVERSE_CEX_SOL_RESERVE", "0.05"))

            from_quote = max(0.0, sol_received * (1.0 - sell_reserve_pct) - sell_reserve)
            from_chain = max(0.0, chain_sol * (1.0 - sell_reserve_pct) - sell_reserve)
            target_sell = from_quote
            if chain_sol > 0:
                target_sell = min(from_quote, from_chain) if from_chain > 0 else from_quote

            backpack_synced = False
            if _env_bool("V2_DEPOSIT_SOL_BEFORE_CEX_SELL", True) and target_sell > 0:
                from decimal import Decimal

                from src.v2.config import V2Config
                from src.v2.inventory_manager import InventoryManager

                post_settle = float(
                    os.getenv("V2_POST_BUY_CEX_DEPOSIT_SETTLE_SEC", "60")
                )
                inv = InventoryManager(
                    V2Config.from_env(),
                    backpack=self.backpack,
                )
                backpack_synced = await inv.ensure_cex_sol(
                    Decimal(str(target_sell)),
                    self.backpack,
                    wallet_pubkey=self.wallet_pubkey,
                    settle_sec=post_settle,
                    after_jupiter_buy=True,
                    strong_signal=bool(
                        opportunity.get("strong_signal")
                        or opportunity.get("roundtrip_passed")
                    ),
                    cex_bid=cex_bid_hint if cex_bid_hint > 0 else None,
                )
                logger.info(
                    "POST_BUY_BACKPACK_SYNC | target_sell=%.6f synced=%s",
                    target_sell,
                    backpack_synced,
                )

            backpack_sol = 0.0
            try:
                if hasattr(self.backpack, "clear_balance_cache"):
                    self.backpack.clear_balance_cache("SOL")
                backpack_sol = float(
                    await self.backpack.get_balance("SOL", force_refresh=True)
                )
            except Exception as exc:
                logger.warning("Backpack SOL balance read failed: %s", exc)
            available_cex = max(0.0, backpack_sol - cex_reserve)
            if backpack_synced:
                sell_sol = target_sell
            else:
                sell_sol = min(target_sell, available_cex)

            logger.info(
                "CEX sell sizing | jupiter_sol=%.6f chain_sol=%.6f target=%.6f "
                "backpack=%.6f sell=%.6f",
                sol_received,
                chain_sol,
                target_sell,
                backpack_sol,
                sell_sol,
            )

            if sell_sol <= 0:
                return {
                    "status": "cex_sell_failed",
                    "error": "insufficient_backpack_sol",
                    "backpack_sol": backpack_sol,
                    "target_sell_sol": target_sell,
                    "sol_received_onchain": sol_received,
                    "chain_sol": chain_sol,
                }
            if sell_sol < target_sell * 0.5:
                logger.warning(
                    "CEX sell capped by Backpack inventory | target=%.6f available=%.6f backpack=%.6f",
                    target_sell,
                    sell_sol,
                    backpack_sol,
                )

            executor = BackpackExecutor(self.backpack)
            safe_sol_qty = await executor.sanitize_sol_quantity(
                sell_sol,
                symbol=str(bp_market),
            )
            if safe_sol_qty <= 0:
                return {
                    "status": "cex_sell_failed",
                    "error": "quantity_too_small_after_sanitize",
                    "jupiter_error": "quantity_too_small_after_sanitize",
                    "jupiter_step": "cex_sell_failed",
                    "sell_sol": sell_sol,
                    "backpack_sol": backpack_sol,
                }
            logger.info(
                "CEX sell preflight | raw=%.8f safe=%.8f market=%s",
                sell_sol,
                safe_sol_qty,
                bp_market,
            )

            sell_result = await executor.sell_sol(
                safe_sol_qty,
                price=cex_bid_hint if cex_bid_hint > 0 else None,
                symbol=str(bp_market),
            )
            if not sell_result.get("success"):
                record_trade_execution("dex_cex_reverse", success=False, pnl_usd=0.0)
                err = str(sell_result.get("error") or "cex_sell_failed")
                return {
                    "status": "cex_sell_failed",
                    "error": err,
                    "jupiter_error": err,
                    "jupiter_step": "cex_sell_failed",
                    "sell_sol": safe_sol_qty,
                    "backpack_sol": backpack_sol,
                }

            usdc_received = float(sell_result.get("usdc_received") or 0)
            usdc_spent = size_usdc_micro / 1_000_000.0
            if usdc_received <= 0:
                usdc_received = (float(opportunity.get("net_bps") or 0) / 10000.0) * usdc_spent

            profit_usdc = (
                usdc_received - usdc_spent
                if usdc_received > 0
                else (float(opportunity.get("net_bps") or 0) / 10000.0) * usdc_spent
            )
            v2_handles_pnl = bool(opportunity.get("v2_handles_pnl"))
            if not v2_handles_pnl:
                self.risk.record_trade_result(profit_usdc)
                record_trade_execution("dex_cex_reverse", success=True, pnl_usd=profit_usdc)

            tx_sig = str(
                buy_result.get("tx_sig")
                or buy_result.get("txid")
                or buy_result.get("bundle_id")
                or ""
            )
            if not v2_handles_pnl:
                try:
                    from src.execution.trade_logger import log_execution_trade

                    log_execution_trade(
                        pair=str(opportunity.get("pair_label") or "SOL/USDC"),
                        gross_bps=gross_bps,
                        net_bps=float(opportunity.get("net_bps") or 0),
                        size_usdc=usdc_spent,
                        success=True,
                        realized_usdc=profit_usdc,
                        tx_sig=tx_sig,
                        strategy="dex_cex_reverse",
                    )
                except Exception as exc:
                    logger.debug("dex_cex_reverse trade log skipped: %s", exc)

            logger.info(
                "FULL ROUNDTRIP | Jupiter OK + CEX sell OK | qty=%.8f usdc_recv=%.4f "
                "usdc_spent=%.4f net=%.4f tx=%s",
                safe_sol_qty,
                usdc_received,
                usdc_spent,
                profit_usdc,
                tx_sig,
            )
            return {
                "status": "success",
                "live_fill": True,
                "path": "dex_cex_reverse",
                "gross_bps": gross_bps,
                "realized_usdc": profit_usdc,
                "usdc_received": usdc_received,
                "usdc_spent_jupiter": usdc_spent,
                "tx_sig": tx_sig,
                "sol_sold": safe_sol_qty,
            }
        except Exception as exc:
            logger.error("CEX sell execution failed: %s", exc, exc_info=True)
            record_trade_execution("dex_cex_reverse", success=False, pnl_usd=0.0)
            return {
                "status": "cex_sell_failed",
                "error": str(exc),
                "jupiter_error": str(exc),
                "jupiter_step": "cex_sell_failed",
            }

    async def detect_and_execute(self) -> dict[str, Any]:
        """Legacy entrypoint (no brain score)."""
        return await self.scan_and_execute(brain_score=0.0)
