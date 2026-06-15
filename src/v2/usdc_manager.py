"""On-chain USDC inventory for v2 reverse (Jupiter buy leg)."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from solders.pubkey import Pubkey

from src.core.wallet import get_wallet_pubkey
from typing import Any

from src.v2.config import V2Config

logger = logging.getLogger(__name__)

USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")


class USDCManager:
    """Preflight and dynamic trade sizing from SPL USDC on the Ledger wallet."""

    def __init__(self, cfg: V2Config) -> None:
        self.config = cfg
        self.min_usdc = float(cfg.min_usdc_balance)
        self.min_trade_usdc = float(cfg.min_trade_usdc)
        self._last_withdraw_error = ""
        pubkey = get_wallet_pubkey()
        self.wallet_pubkey = Pubkey.from_string(pubkey) if pubkey else None

    async def get_available_usdc(self) -> float:
        """On-chain SPL USDC with RPC fallback, cache, and stale-read protection."""
        if self.wallet_pubkey is None:
            logger.warning("NO_WALLET_PUBKEY — cannot read on-chain USDC")
            return 0.0

        from src.core.wallet import get_usdc_balance_robust

        try:
            usdc = float(
                await get_usdc_balance_robust(str(self.wallet_pubkey))
            )
            if usdc >= self.min_usdc:
                logger.info(
                    "USDC BALANCE OK | $%.2f on-chain (min $%.2f)",
                    usdc,
                    self.min_usdc,
                )
            else:
                logger.warning(
                    "USDC BALANCE LOW | $%.2f on-chain (min $%.2f)",
                    usdc,
                    self.min_usdc,
                )
            return usdc
        except Exception as exc:
            logger.error("USDC check failed: %s", exc)
            return 0.0

    def has_minimum(self, available_usdc: float) -> bool:
        return available_usdc >= self.min_usdc

    def _env_bool(self, name: str, default: bool = False) -> bool:
        raw = (os.getenv(name) or "").strip().lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes", "on")

    async def _wait_for_usdc_target(
        self,
        target: float,
        *,
        timeout_sec: float,
        poll_sec: float = 5.0,
    ) -> float:
        """Poll on-chain USDC until ``target`` or timeout."""
        deadline = time.monotonic() + max(5.0, timeout_sec)
        latest = await self.get_available_usdc()
        while time.monotonic() < deadline:
            if latest >= target:
                return latest
            await asyncio.sleep(poll_sec)
            latest = await self.get_available_usdc()
        return latest

    def _withdraw_needs_2fa(self, result: dict[str, Any] | None) -> bool:
        err = str((result or {}).get("error") or "").lower()
        return "2fa" in err or "two factor" in err or "twofactor" in err

    def replenish_block_reason(self) -> str:
        """Human/log block reason after a failed replenish attempt."""
        if self._withdraw_needs_2fa({"error": self._last_withdraw_error}):
            return "cex_withdraw_2fa_required"
        return "insufficient_usdc"

    async def _replenish_from_onchain_sol(
        self,
        jupiter: Any,
        *,
        available: float,
        target: float,
    ) -> float:
        """Swap on-chain SOL → USDC when CEX withdraw is blocked (e.g. Backpack 2FA)."""
        if not self._env_bool("V2_AUTO_SWAP_SOL_FOR_USDC", True):
            return available

        try:
            from src.core.capital_preflight import get_ledger_sol_balance

            chain_sol = float(await get_ledger_sol_balance())
        except Exception as exc:
            logger.warning("SOL balance read failed for replenish: %s", exc)
            return available

        reserve = float(os.getenv("V2_SOL_REPLENISH_RESERVE", "0.15"))
        sellable = max(0.0, chain_sol - reserve)
        if sellable < 0.05:
            logger.warning(
                "SOL replenish skipped | chain_sol=%.4f reserve=%.4f",
                chain_sol,
                reserve,
            )
            return available

        need_usdc = max(0.0, target - available)
        if need_usdc < 1.0:
            return available

        cex_bid = 0.0
        try:
            from src.cex.backpack import get_backpack_client

            cex_bid = float(await get_backpack_client().get_bid_price("SOL") or 0)
        except Exception as exc:
            logger.debug("CEX bid for SOL replenish unavailable: %s", exc)
        if cex_bid <= 0:
            cex_bid = float(os.getenv("V2_SOL_REPLENISH_PRICE_FALLBACK", "65"))

        sol_to_sell = min(sellable, (need_usdc / cex_bid) * 1.08)
        lamports = max(1, int(sol_to_sell * 1_000_000_000))
        logger.info(
            "USDC replenish | Jupiter SOL→USDC | sell=%.4f SOL need=$%.2f on_chain_usdc=$%.2f",
            sol_to_sell,
            need_usdc,
            available,
        )
        try:
            result = await jupiter.sell_sol(
                lamports,
                slippage_bps=int(os.getenv("V2_SOL_REPLENISH_SLIPPAGE_BPS", "80")),
                rpc_only=self._env_bool("V2_REPLENISH_RPC_ONLY", True),
            )
            if not result.get("success"):
                logger.warning("SOL→USDC replenish failed: %s", result.get("error"))
                return available
            out_micro = int(result.get("out_usdc_micro") or 0)
            estimated = available + (out_micro / 1_000_000.0)
            settle = float(os.getenv("V2_SOL_REPLENISH_SETTLE_SEC", "20"))
            updated = await self._wait_for_usdc_target(
                self.min_trade_usdc,
                timeout_sec=settle,
                poll_sec=3.0,
            )
            if updated >= self.min_trade_usdc:
                return updated
            if estimated >= self.min_trade_usdc:
                logger.info(
                    "USDC replenish | using swap quote estimate $%.2f (on-chain read=$%.2f)",
                    estimated,
                    updated,
                )
                return estimated
            return max(updated, estimated)
        except Exception as exc:
            logger.warning("SOL→USDC replenish error: %s", exc)
            return available

    async def replenish_usdc_for_trade(
        self,
        backpack: Any,
        jupiter: Any | None = None,
        *,
        wallet_pubkey: str | None = None,
    ) -> tuple[float, str]:
        """
        Top up on-chain USDC via Backpack withdraw, then SOL→USDC swap fallback.

        Returns (available_usdc, replenish_note).
        """
        available = await self.replenish_from_backpack_if_needed(
            backpack,
            wallet_pubkey=wallet_pubkey,
        )
        note = ""
        if available >= self.min_trade_usdc:
            return available, note

        if jupiter is not None:
            target = float(
                os.getenv("V2_CEX_USDC_WITHDRAW_TARGET", str(self.min_usdc))
            )
            updated = await self._replenish_from_onchain_sol(
                jupiter,
                available=available,
                target=target,
            )
            if updated > available:
                return updated, "sol_swap_replenish"
            if updated >= self.min_trade_usdc:
                return updated, "sol_swap_replenish"

        if self._withdraw_needs_2fa({"error": self._last_withdraw_error}):
            note = "cex_withdraw_2fa_required"
        return available, note

    async def replenish_from_backpack_if_needed(
        self,
        backpack: Any,
        *,
        wallet_pubkey: str | None = None,
    ) -> float:
        """
        Withdraw USDC from Backpack when on-chain balance is below min trade.

        Keeps reverse arb running after Jupiter spends wallet USDC on the buy leg.
        """
        if not self._env_bool("V2_AUTO_WITHDRAW_USDC_FROM_CEX", True):
            return await self.get_available_usdc()

        available = await self.get_available_usdc()
        target = float(os.getenv("V2_CEX_USDC_WITHDRAW_TARGET", str(self.min_usdc)))
        if available >= self.min_usdc:
            return available

        try:
            cex_usdc = float(await backpack.get_balance("USDC"))
        except Exception as exc:
            logger.warning("CEX USDC balance read failed: %s", exc)
            return available

        reserve = float(os.getenv("V2_CEX_USDC_RESERVE", "20"))
        withdrawable = max(0.0, cex_usdc - reserve)
        need = max(0.0, target - available)
        amount = min(need, withdrawable, float(self.config.max_trade_usdc))
        if amount < 1.0:
            logger.warning(
                "USDC replenish skipped | on_chain=$%.2f cex=$%.2f need=$%.2f withdrawable=$%.2f",
                available,
                cex_usdc,
                need,
                withdrawable,
            )
            return available

        dest = (
            wallet_pubkey
            or os.getenv("WALLET_PUBKEY")
            or get_wallet_pubkey()
            or ""
        ).strip()
        if not dest:
            logger.warning("USDC replenish skipped — WALLET_PUBKEY missing")
            return available

        logger.info(
            "USDC replenish | withdrawing $%.2f from Backpack -> %s… (on_chain=$%.2f cex=$%.2f)",
            amount,
            dest[:12],
            available,
            cex_usdc,
        )
        try:
            result = await backpack.withdraw_usdc(amount, dest)
            if not result.get("success"):
                err = str(result.get("error") or result)
                self._last_withdraw_error = err
                if self._withdraw_needs_2fa(result):
                    logger.warning(
                        "CEX USDC withdraw needs 2FA-exempt address | whitelist %s… "
                        "at backpack.exchange/settings/withdrawal-addresses — "
                        "falling back to SOL→USDC if enabled",
                        dest[:12],
                    )
                else:
                    logger.warning("CEX USDC withdraw failed: %s", err)
                return available
            self._last_withdraw_error = ""
            settle = float(os.getenv("V2_CEX_USDC_WITHDRAW_SETTLE_SEC", "45"))
            updated = await self._wait_for_usdc_target(
                self.min_trade_usdc,
                timeout_sec=settle,
            )
            logger.info(
                "USDC replenish settle | on_chain=$%.2f target=$%.2f",
                updated,
                self.min_trade_usdc,
            )
            return updated
        except Exception as exc:
            logger.warning("CEX USDC withdraw error: %s", exc)
            return available

    def trade_size_micro(self, available_usdc: float, signal_size_micro: int) -> int:
        """Cap size with 15% buffer; floor at min viable trade."""
        cap_usdc = max(0.0, int(signal_size_micro) / 1_000_000.0)
        safe_usdc = min(available_usdc * 0.85, cap_usdc)
        if safe_usdc < self.min_trade_usdc and available_usdc >= self.min_trade_usdc:
            safe_usdc = min(cap_usdc, available_usdc * 0.85)
        elif safe_usdc < self.min_trade_usdc:
            return 0
        return max(0, int(safe_usdc * 1_000_000))
