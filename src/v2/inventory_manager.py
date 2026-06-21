"""
src/v2/inventory_manager.py

Unified inventory management for v2 reverse arb.

Replenish order (production):
  1. Wallet SOL → Backpack deposit (primary)
  2. Backpack USDC → SOL market buy (gated fallback: STRONG / post-Jupiter, low SOL)
  3. Orphan: wallet deposit → Jupiter SOL→USDC

USDC sizing/replenish delegates to ``USDCManager``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from decimal import Decimal
from typing import Any

from src.v2.config import V2Config
from src.v2.usdc_manager import USDCManager

logger = logging.getLogger(__name__)


class InventoryManager:
    """
    Coordinates wallet USDC (Jupiter buy leg) and Backpack SOL (CEX sell leg).

    USDC flows delegate to ``USDCManager``. SOL preflight deposits wallet SOL to
    Backpack when CEX inventory is low; orphan recovery runs after a failed CEX sell.
    """

    def __init__(self, cfg: V2Config, backpack: Any | None = None) -> None:
        self.config = cfg
        self.usdc = USDCManager(cfg)
        self.backpack = backpack
        self.min_sol_reserve = Decimal(
            os.getenv("DEX_CEX_REVERSE_CEX_SOL_RESERVE", "0.08")
        )
        self.target_backpack_sol = Decimal(
            os.getenv("V2_TARGET_BACKPACK_SOL", "0.35")
        )
        self._deposit_address_cache: str = ""
        self.max_backpack_swap_usdc = Decimal(
            os.getenv(
                "V2_MAX_REPLENISH_USDC",
                os.getenv("V2_BACKPACK_SWAP_MAX_USDC", "12"),
            )
        )
        self.swap_slippage_bps = int(
            os.getenv(
                "V2_BACKPACK_SWAP_SLIPPAGE_BPS",
                os.getenv("BACKPACK_SWAP_SLIPPAGE_BPS", "80"),
            )
        )

    @property
    def target_sol(self) -> Decimal:
        """Alias for ``target_backpack_sol`` (docs / monitoring)."""
        return self.target_backpack_sol

    @property
    def max_replenish_usdc(self) -> Decimal:
        """Alias for ``max_backpack_swap_usdc`` cap."""
        return self.max_backpack_swap_usdc

    # --- USDC delegation (backward compatible with ``usdc_manager``) ---

    def _backpack_swap_trigger_sol(self) -> Decimal:
        return Decimal(os.getenv("V2_BACKPACK_SOL_SWAP_TRIGGER", "0.08"))

    def _should_try_backpack_swap_replenish(
        self,
        cex_sol: Decimal,
        *,
        strong_signal: bool = False,
        after_jupiter_buy: bool = False,
    ) -> bool:
        """
        Fallback only: STRONG signal or post-Jupiter buy, critically low Backpack SOL.
        """
        if not self._env_bool("V2_BACKPACK_SWAP_SOL_ENABLED", True):
            return False
        if not (strong_signal or after_jupiter_buy):
            logger.debug("Backpack USDC→SOL swap skipped — not STRONG/post-buy")
            return False
        if cex_sol >= self._backpack_swap_trigger_sol():
            logger.debug(
                "Backpack USDC→SOL swap skipped — SOL %.6f >= trigger %.6f",
                float(cex_sol),
                float(self._backpack_swap_trigger_sol()),
            )
            return False
        return True

    async def _swap_usdc_to_sol_on_backpack(
        self,
        backpack: Any,
        target_sol: Decimal,
        *,
        cex_bid: float | None = None,
    ) -> dict[str, Any]:
        """
        Market-buy SOL on Backpack using CEX USDC (fallback replenish only).

        Size-capped to preserve arb edge (~5–15 bps CEX cost).
        """
        sym = (
            os.getenv("CEX_DEX_BACKPACK_MARKET")
            or os.getenv("V2_BACKPACK_SWAP_MARKET")
            or "SOL_USDC"
        )
        min_usdc = Decimal(str(os.getenv("V2_BACKPACK_SWAP_MIN_USDC", "8")))
        usdc_frac = Decimal(str(os.getenv("V2_BACKPACK_SWAP_USDC_FRACTION", "0.4")))
        usdc_reserve = Decimal(str(os.getenv("V2_BACKPACK_SWAP_USDC_RESERVE", "50")))
        slip_bps = int(
            os.getenv(
                "V2_BACKPACK_SWAP_SLIPPAGE_BPS",
                os.getenv("BACKPACK_SWAP_SLIPPAGE_BPS", "80"),
            )
        )
        slip_mult = Decimal("1") + Decimal(slip_bps) / Decimal("10000")

        try:
            usdc_bal = Decimal(
                str(await backpack.get_balance("USDC", force_refresh=True))
            )
        except Exception as exc:
            return {"success": False, "error": f"usdc_balance_read_failed:{exc}"}

        spendable = usdc_bal - usdc_reserve
        if spendable < min_usdc:
            logger.warning(
                "Backpack swap skipped — spendable USDC $%.2f (bal=$%.2f reserve=$%.2f)",
                float(spendable),
                float(usdc_bal),
                float(usdc_reserve),
            )
            return {"success": False, "error": "insufficient_backpack_usdc"}

        price = Decimal(str(cex_bid or 0))
        if price <= 0:
            try:
                ask = await backpack.get_best_ask(sym)
                if ask and float(ask) > 0:
                    price = Decimal(str(ask))
            except Exception:
                price = Decimal("0")
        if price <= 0:
            try:
                mid = await backpack.get_sol_usdc_price()
                if mid and float(mid) > 0:
                    price = Decimal(str(mid))
            except Exception:
                price = Decimal("0")
        if price <= 0:
            price = Decimal(os.getenv("V2_SOL_REPLENISH_PRICE_FALLBACK", "150"))

        needed_usdc = target_sol * price * slip_mult * Decimal("1.03")
        swap_usdc = min(
            self.max_backpack_swap_usdc,
            spendable * usdc_frac,
            needed_usdc,
        )
        if swap_usdc < min_usdc:
            logger.warning(
                "Backpack swap size too small | swap_usdc=$%.2f need=$%.2f target_sol=%.6f",
                float(swap_usdc),
                float(needed_usdc),
                float(target_sol),
            )
            return {"success": False, "error": "swap_size_too_small"}

        est_sol = swap_usdc / (price * slip_mult)
        if est_sol < target_sol * Decimal("0.85"):
            logger.warning(
                "Backpack swap unlikely to cover gap | est_sol=%.6f target=%.6f usdc=$%.2f",
                float(est_sol),
                float(target_sol),
                float(swap_usdc),
            )
            return {"success": False, "error": "swap_covers_insufficient_sol"}

        size_micro = int(swap_usdc * Decimal(1_000_000))
        logger.info(
            "BACKPACK_SWAP_REPLENISH | usdc=$%.2f est_sol≈%.6f target_sol=%.6f price=%.4f",
            float(swap_usdc),
            float(est_sol),
            float(target_sol),
            float(price),
        )

        try:
            result = await backpack.place_market_buy(sym, size_micro)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        if not result.get("success"):
            return {
                "success": False,
                "error": str(result.get("error") or "backpack_buy_failed"),
                "raw": result,
            }

        settle = float(os.getenv("V2_BACKPACK_SWAP_SETTLE_SEC", "8"))
        await asyncio.sleep(settle)
        if hasattr(backpack, "clear_balance_cache"):
            backpack.clear_balance_cache("SOL")
            backpack.clear_balance_cache("USDC")
        sol_after = await self.get_backpack_sol(backpack)
        logger.info(
            "BACKPACK_SWAP_OK | usdc=$%.2f sol_after=%.6f order=%s",
            float(swap_usdc),
            sol_after,
            result.get("orderId") or result.get("id"),
        )
        return {
            "success": True,
            "recovery_path": "backpack_usdc_to_sol",
            "usdc_spent": float(swap_usdc),
            "sol_received_est": float(est_sol),
            "backpack_sol_after": sol_after,
            "order": result,
        }

    @property
    def min_usdc(self) -> float:
        return self.usdc.min_usdc

    @property
    def min_trade_usdc(self) -> float:
        return self.usdc.min_trade_usdc

    async def get_available_usdc(self) -> float:
        return await self.usdc.get_available_usdc()

    def has_minimum(self, available_usdc: float) -> bool:
        return self.usdc.has_minimum(available_usdc)

    def replenish_block_reason(self) -> str:
        return self.usdc.replenish_block_reason()

    async def replenish_usdc_for_trade(
        self,
        backpack: Any,
        jupiter: Any | None = None,
        *,
        wallet_pubkey: str | None = None,
        signal: dict[str, Any] | None = None,
        allow_replenish: bool | None = None,
    ) -> tuple[float, str]:
        return await self.usdc.replenish_usdc_for_trade(
            backpack,
            jupiter,
            wallet_pubkey=wallet_pubkey,
            signal=signal,
            allow_replenish=allow_replenish,
        )

    def trade_size_micro(self, available_usdc: float, signal_size_micro: int) -> int:
        return self.usdc.trade_size_micro(available_usdc, signal_size_micro)

    # --- SOL inventory ---

    def _env_bool(self, name: str, default: bool = True) -> bool:
        raw = (os.getenv(name) or "").strip().lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes", "on")

    async def get_backpack_sol(self, backpack: Any | None = None) -> float:
        client = backpack or self.backpack
        if client is None:
            return 0.0
        try:
            return float(await client.get_balance("SOL", force_refresh=True))
        except Exception as exc:
            logger.warning("Backpack SOL balance read failed: %s", exc)
            return 0.0

    async def get_wallet_sol(self, wallet_pubkey: str | None = None) -> float:
        from src.core.rpc_config import get_robust_sol_balance

        try:
            return float(await get_robust_sol_balance(wallet_pubkey))
        except Exception as exc:
            logger.warning("Wallet SOL balance read failed: %s", exc)
            return 0.0

    async def is_inventory_healthy(
        self,
        backpack: Any | None = None,
        *,
        wallet_pubkey: str | None = None,
        trade_usdc_micro: int | None = None,
        cex_bid: float | None = None,
    ) -> bool:
        """
        True when on-chain USDC and Backpack SOL can support the next reverse trade.

        Used to relax adaptive gates when capital is not the bottleneck.
        """
        usdc = await self.get_available_usdc()
        if usdc < self.min_trade_usdc:
            return False

        client = backpack or self.backpack
        if client is None:
            return usdc >= self.min_usdc

        micro = int(trade_usdc_micro or self.config.max_trade_usdc_micro)
        cex_sol = await self.get_backpack_sol(client)
        target = float(self.target_backpack_sol)
        health_frac = float(os.getenv("V2_INVENTORY_SOL_HEALTH_FRAC", "0.5"))

        # Wallet-first + post-buy deposit: CEX sell SOL comes from Jupiter, not
        # pre-staged Backpack inventory — only require reserve + target buffer.
        if self._env_bool("V2_DEPOSIT_SOL_BEFORE_CEX_SELL", True):
            need_sol = float(self.min_sol_reserve) + 0.05
            healthy = cex_sol >= need_sol and cex_sol >= target * health_frac
        else:
            required = self.estimate_required_cex_sol(micro, cex_bid=cex_bid)
            need_sol = float(required) + float(self.min_sol_reserve)
            healthy = cex_sol >= need_sol and (
                cex_sol >= target * health_frac or cex_sol >= need_sol * 1.5
            )
        if not healthy:
            logger.debug(
                "inventory_unhealthy | usdc=%.2f cex_sol=%.6f need=%.6f target=%.3f",
                usdc,
                cex_sol,
                need_sol,
                target,
            )
        return healthy

    def estimate_required_cex_sol(
        self,
        trade_usdc_micro: int,
        *,
        cex_bid: float | None = None,
        sell_reserve_pct: float | None = None,
    ) -> Decimal:
        """Rough SOL needed on Backpack for the CEX sell leg."""
        usdc = Decimal(trade_usdc_micro) / Decimal(1_000_000)
        bid = Decimal(str(cex_bid or os.getenv("V2_SOL_REPLENISH_PRICE_FALLBACK", "65")))
        if bid <= 0:
            bid = Decimal("65")
        pct = Decimal(
            str(sell_reserve_pct or os.getenv("DEX_CEX_REVERSE_SELL_RESERVE_PCT", "0.03"))
        )
        gross_sol = usdc / bid
        return gross_sol * (Decimal("1") - pct)

    async def _wait_for_backpack_sol(
        self,
        backpack: Any,
        target: Decimal,
        *,
        timeout_sec: float,
        poll_sec: float = 5.0,
    ) -> float:
        deadline = time.monotonic() + max(5.0, timeout_sec)
        latest = await self.get_backpack_sol(backpack)
        while time.monotonic() < deadline:
            if Decimal(str(latest)) >= target:
                return latest
            await asyncio.sleep(poll_sec)
            latest = await self.get_backpack_sol(backpack)
        return latest

    async def _backpack_deposit_address(self, backpack: Any) -> str:
        if self._deposit_address_cache:
            return self._deposit_address_cache
        result = await backpack.get_deposit_address("Solana")
        if not result.get("success"):
            raise RuntimeError(str(result.get("error") or "deposit_address_failed"))
        address = str(result.get("address") or "").strip()
        if not address:
            raise RuntimeError("deposit_address_empty")
        self._deposit_address_cache = address
        return address

    async def deposit_wallet_sol_to_backpack(
        self,
        backpack: Any,
        amount_sol: float,
        *,
        wallet_pubkey: str | None = None,
        settle_sec: float | None = None,
    ) -> dict[str, Any]:
        """Transfer on-chain SOL to Backpack deposit address."""
        if amount_sol <= 0:
            return {"success": False, "error": "amount_zero"}

        wallet_sol = await self.get_wallet_sol(wallet_pubkey)
        chain_reserve = float(os.getenv("V2_SOL_TRANSFER_RESERVE", "0.15"))
        if wallet_sol - amount_sol < chain_reserve:
            return {
                "success": False,
                "error": "insufficient_wallet_sol",
                "wallet_sol": wallet_sol,
                "amount_sol": amount_sol,
                "chain_reserve": chain_reserve,
            }

        try:
            dest = await self._backpack_deposit_address(backpack)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        from src.core.wallet import transfer_sol

        logger.info(
            "SOL deposit to Backpack | amount=%.6f dest=%s… wallet=%.6f",
            amount_sol,
            dest[:12],
            wallet_sol,
        )
        result = await transfer_sol(amount_sol, dest)
        if not result.get("success"):
            return result

        settle = float(
            settle_sec
            if settle_sec is not None
            else os.getenv("V2_CEX_SOL_DEPOSIT_SETTLE_SEC", "45")
        )
        target = await self.get_backpack_sol(backpack) + amount_sol * 0.98
        updated = await self._wait_for_backpack_sol(
            backpack,
            Decimal(str(target)),
            timeout_sec=settle,
        )
        result["backpack_sol_after"] = updated
        if hasattr(backpack, "clear_balance_cache"):
            backpack.clear_balance_cache("SOL")
        return result

    async def ensure_cex_sol(
        self,
        required_sol: Decimal | float,
        backpack: Any | None = None,
        *,
        wallet_pubkey: str | None = None,
        settle_sec: float | None = None,
        strong_signal: bool = False,
        after_jupiter_buy: bool = False,
        cex_bid: float | None = None,
    ) -> bool:
        """
        Pre-flight: ensure Backpack holds enough SOL for the CEX sell leg.

        Deposits from the on-chain wallet when below ``required + min_reserve``.
        """
        client = backpack or self.backpack
        if client is None:
            logger.warning("ensure_cex_sol skipped — no Backpack client")
            return False

        required = Decimal(str(required_sol))
        if required <= 0:
            return True

        auto_replenish = self._env_bool(
            "ENABLE_BACKPACK_AUTO_REPLENISH",
            self._env_bool("V2_AUTO_DEPOSIT_SOL_TO_CEX", True),
        )
        if not auto_replenish:
            cex_sol = Decimal(str(await self.get_backpack_sol(client)))
            ok = cex_sol >= required + self.min_sol_reserve
            if not ok:
                logger.warning(
                    "Backpack SOL low | cex=%.6f need=%.6f reserve=%.6f "
                    "(auto-deposit disabled)",
                    float(cex_sol),
                    float(required),
                    float(self.min_sol_reserve),
                )
            return ok

        cex_sol = Decimal(str(await self.get_backpack_sol(client)))
        need_total = required + self.min_sol_reserve
        logger.info(
            "Backpack SOL balance: %.4f (required ~%.4f + reserve %.4f)",
            float(cex_sol),
            float(required),
            float(self.min_sol_reserve),
        )
        if cex_sol >= need_total:
            logger.info(
                "Backpack SOL OK | cex=%.6f need=%.6f reserve=%.6f",
                float(cex_sol),
                float(required),
                float(self.min_sol_reserve),
            )
            return True

        needed = need_total - cex_sol
        logger.warning(
            "Low Backpack SOL: %.6f (need %.6f + reserve %.6f, gap %.6f). "
            "Depositing from wallet…",
            float(cex_sol),
            float(required),
            float(self.min_sol_reserve),
            float(needed),
        )

        gap = max(
            need_total - cex_sol,
            self.target_backpack_sol - cex_sol,
            Decimal("0.05"),
        )
        deposit_amount = float(gap.quantize(Decimal("0.01")))
        result = await self.deposit_wallet_sol_to_backpack(
            client,
            deposit_amount,
            wallet_pubkey=wallet_pubkey,
            settle_sec=settle_sec,
        )
        if not result.get("success"):
            logger.warning("Backpack SOL deposit failed: %s", result.get("error"))
            if self._should_try_backpack_swap_replenish(
                cex_sol,
                strong_signal=strong_signal,
                after_jupiter_buy=after_jupiter_buy,
            ):
                gap = need_total - cex_sol
                swap = await self._swap_usdc_to_sol_on_backpack(
                    client,
                    gap,
                    cex_bid=cex_bid,
                )
                if swap.get("success"):
                    updated = Decimal(str(swap.get("backpack_sol_after") or 0))
                    if updated <= 0:
                        updated = Decimal(str(await self.get_backpack_sol(client)))
                    ok = updated >= need_total
                    logger.info(
                        "Backpack SOL after swap fallback | cex=%.6f target=%.6f ok=%s",
                        float(updated),
                        float(need_total),
                        ok,
                    )
                    return ok
            return False

        updated = Decimal(str(result.get("backpack_sol_after") or 0))
        if updated <= 0:
            updated = Decimal(str(await self.get_backpack_sol(client)))
        ok = updated >= need_total
        tx_sig = result.get("tx_sig")
        if not ok and tx_sig:
            # Transfer landed on-chain; Backpack credit can lag the settle window.
            trust = self._env_bool("V2_CEX_SOL_TRUST_ONCHAIN_DEPOSIT", True)
            if trust:
                logger.warning(
                    "Backpack SOL settle lag | cex=%.6f target=%.6f tx=%s "
                    "(trusting on-chain deposit)",
                    float(updated),
                    float(need_total),
                    tx_sig,
                )
                ok = True
        logger.info(
            "Backpack SOL after deposit | cex=%.6f target=%.6f ok=%s tx=%s",
            float(updated),
            float(need_total),
            ok,
            result.get("tx_sig"),
        )
        return ok

    async def recover_orphan(
        self,
        tx_sig: str,
        sol_received: Decimal | float,
        backpack: Any | None = None,
        *,
        wallet_pubkey: str | None = None,
        jupiter: Any | None = None,
    ) -> dict[str, Any]:
        """
        Post-failed-CEX: move Jupiter-received SOL to Backpack (or swap to USDC).

        Called when Jupiter buy succeeded but the Backpack sell leg failed.
        """
        if not self._env_bool("V2_AUTO_RECOVER_ORPHAN_SOL", True):
            return {"success": False, "error": "orphan_recovery_disabled"}

        amount = float(Decimal(str(sol_received)))
        if amount <= 0:
            return {"success": False, "error": "zero_sol_received"}

        sell_reserve = float(os.getenv("DEX_CEX_REVERSE_SELL_RESERVE_SOL", "0.01"))
        deposit_amount = max(0.0, amount - sell_reserve)
        if deposit_amount < 0.01:
            return {"success": False, "error": "orphan_amount_too_small"}

        client = backpack or self.backpack
        logger.info(
            "Recovering orphan %s — depositing %.6f SOL to Backpack",
            tx_sig[:16] if tx_sig else "n/a",
            deposit_amount,
        )

        if client is not None:
            result = await self.deposit_wallet_sol_to_backpack(
                client,
                deposit_amount,
                wallet_pubkey=wallet_pubkey,
            )
            if result.get("success"):
                result["recovery_path"] = "backpack_deposit"
                return result
            logger.warning(
                "Orphan Backpack deposit failed (%s) — trying Jupiter SOL→USDC",
                result.get("error"),
            )

        if jupiter is not None and self._env_bool("V2_AUTO_SWAP_SOL_FOR_USDC", True):
            lamports = max(1, int(deposit_amount * 1_000_000_000))
            try:
                swap = await jupiter.sell_sol(
                    lamports,
                    slippage_bps=int(os.getenv("V2_SOL_REPLENISH_SLIPPAGE_BPS", "80")),
                    rpc_only=self._env_bool("V2_REPLENISH_RPC_ONLY", True),
                )
                if swap.get("success"):
                    return {
                        "success": True,
                        "recovery_path": "jupiter_sol_usdc",
                        "tx_sig": tx_sig,
                        "out_usdc_micro": swap.get("out_usdc_micro"),
                    }
                return {
                    "success": False,
                    "error": str(swap.get("error") or "jupiter_sell_failed"),
                    "recovery_path": "jupiter_sol_usdc",
                }
            except Exception as exc:
                return {"success": False, "error": str(exc), "recovery_path": "jupiter_sol_usdc"}

        return {"success": False, "error": "no_recovery_path"}

    async def get_inventory_snapshot(
        self,
        backpack: Any | None = None,
        *,
        wallet_pubkey: str | None = None,
    ) -> dict[str, Any]:
        """Current Backpack + wallet balances for logging and monitoring."""
        client = backpack or self.backpack
        backpack_sol = 0.0
        backpack_usdc = 0.0
        if client is not None:
            try:
                backpack_sol = await self.get_backpack_sol(client)
            except Exception as exc:
                logger.debug("snapshot backpack SOL failed: %s", exc)
            try:
                backpack_usdc = float(
                    await client.get_balance("USDC", force_refresh=True)
                )
            except Exception as exc:
                logger.debug("snapshot backpack USDC failed: %s", exc)

        onchain_sol = await self.get_wallet_sol(wallet_pubkey)
        onchain_usdc = await self.get_available_usdc()
        try:
            from src.monitoring.cex_health import record_backpack_balances

            record_backpack_balances(backpack_usdc, backpack_sol)
        except Exception:
            pass
        return {
            "backpack_sol": backpack_sol,
            "backpack_usdc": backpack_usdc,
            "onchain_sol": onchain_sol,
            "onchain_usdc": onchain_usdc,
            "min_sol_reserve": float(self.min_sol_reserve),
            "target_sol": float(self.target_backpack_sol),
            "max_replenish_usdc": float(self.max_backpack_swap_usdc),
            "timestamp": time.time(),
        }
