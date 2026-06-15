"""CCXT-backed CEX <-> DEX discrepancy detector and execution bridge."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import ccxt.async_support as ccxt
from cex_dex_flash_arb import build_and_execute_cex_dex_flash_arb
from cex_price_feed import cex_feed
from get_jupiter_quote import get_jupiter_quote
from jupiter_executor import JupiterExecutor
from openai_helper import ai_agent_decide
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from strategy_cycle_signals import note_cex_dex_context

logger = logging.getLogger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

CEX_DEX_CONFIG = {
    "flash_amount_usdc": int(os.getenv("CEX_DEX_FLASH_AMOUNT_USDC_MICRO", "35000000")),
}

CEX_POLL_INTERVAL = float(os.getenv("CEX_POLL_INTERVAL_SEC", "1.8"))

# Midcap + large-cap symbols with common Jupiter mints.
TOKEN_MINTS = {
    "SOL": SOL_MINT,
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6vY4R6hM3Q9GfQ9",
    "WIF": "EKpQGSJtjMFqKZc2tQxPq1rQxw7xQxw7xQxw7xQxw7x",  # override if needed
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
}


class CexDexArbitrageBot:
    def __init__(self, client: AsyncClient, keypair: Keypair, executor: JupiterExecutor):
        self.client = client
        self.keypair = keypair
        self.executor = executor
        self.config = {
            "min_spread_bps": float(os.getenv("CEX_DEX_MIN_SPREAD_BPS", "35")),
            "ai_min_confidence": int(os.getenv("AI_APPROVE_MIN_CONFIDENCE", "58")),
            "max_trade_usdc_micro": int(os.getenv("CEX_DEX_MAX_TRADE_USDC_MICRO", "150000000")),
        }
        self.MIN_CEX_DEX_SPREAD_BPS = self.config["min_spread_bps"]
        self._binance = ccxt.binance({"enableRateLimit": True})
        self._backpack = ccxt.backpack({"enableRateLimit": True})

    async def close(self) -> None:
        await self._binance.close()
        await self._backpack.close()

    async def _fetch_cex_mid(self, symbol: str) -> tuple[float | None, str]:
        for ex_name, ex in (("backpack", self._backpack), ("binance", self._binance)):
            try:
                ticker = await ex.fetch_ticker(symbol)
                bid = float(ticker.get("bid") or 0.0)
                ask = float(ticker.get("ask") or 0.0)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2.0, ex_name
            except Exception as exc:
                logger.debug("%s ticker failed for %s: %s", ex_name, symbol, exc)
        return None, ""

    async def _fetch_dex_mid(self, base_symbol: str, usdc_micro: int = 1_000_000) -> float | None:
        mint = TOKEN_MINTS.get(base_symbol)
        if not mint:
            return None
        quote = await get_jupiter_quote(USDC_MINT, mint, usdc_micro, slippage_bps=50)
        try:
            out_amount = int((quote or {}).get("outAmount") or 0)
            if out_amount <= 0:
                return None
            # implied USDC per token
            return usdc_micro / out_amount
        except Exception:
            return None

    async def detect_opportunity(self) -> dict[str, Any] | None:
        """Scan CEX vs Jupiter for actionable spreads with clear logging."""
        best: dict[str, Any] | None = None

        logger.debug("[CEX-DEX] Starting scan | min_spread_bps=%s", self.MIN_CEX_DEX_SPREAD_BPS)

        for base in TOKEN_MINTS:
            symbol = f"{base}/USDC"
            cex_mid, cex_source = await self._fetch_cex_mid(symbol)
            if not cex_mid:
                logger.debug(f"[CEX-DEX] No CEX price for {symbol}")
                continue

            dex_mid = await self._fetch_dex_mid(base)
            if not dex_mid:
                logger.debug(f"[CEX-DEX] No DEX price for {symbol}")
                continue

            spread_bps = abs((dex_mid - cex_mid) / cex_mid) * 10_000.0
            direction = "cex_cheap" if cex_mid < dex_mid else "dex_cheap"

            cand = {
                "strategy": "cex_dex_arb",
                "type": "cex_dex_arb",
                "active": spread_bps >= self.MIN_CEX_DEX_SPREAD_BPS,
                "pair": symbol,
                "base_symbol": base,
                "cex_source": cex_source,
                "cex_mid": cex_mid,
                "dex_mid": dex_mid,
                "spread_bps": spread_bps,
                "spread_bps_net": spread_bps,  # will be refined later
                "direction": direction,
                "size_usdc_micro": self.config["max_trade_usdc_micro"],
            }

            if best is None or cand["spread_bps"] > best.get("spread_bps", 0):
                best = cand

        note_cex_dex_context(best or {"active": False})

        if best and best.get("active"):
            logger.info(
                "🔥 CEX-DEX OPPORTUNITY | %s | spread=%.1fbps | dir=%s",
                best["pair"],
                float(best["spread_bps"]),
                best["direction"],
            )
            return best

        logger.debug("No CEX-DEX edge above %sbps", self.MIN_CEX_DEX_SPREAD_BPS)
        return None

    async def execute(self, opp: dict[str, Any]) -> Any:
        balance = (await self.client.get_balance(self.keypair.pubkey())).value
        sym = str(opp.get("pair") or "SOL/USDC")
        cex_prices = await cex_feed.get_multiple_prices([sym])
        opp_with_cex = {**opp, "cex_prices": cex_prices}
        decision = await ai_agent_decide(
            opp_with_cex,
            balance,
            min_confidence=self.config["ai_min_confidence"],
        )
        if decision.get("final_action") != "APPROVE":
            td = (
                decision.get("trade_decision")
                if isinstance(decision.get("trade_decision"), dict)
                else {}
            )
            logger.info(
                "CEX-DEX execute: AI gate — final_action=%s pair=%s confidence=%s",
                decision.get("final_action"),
                opp.get("pair"),
                td.get("confidence"),
            )
            return None

        # Execute SOL lane via flash helper; keep midcaps as signal-only until route plumbing is added.
        if opp.get("base_symbol") != "SOL":
            logger.info(
                "CEX-DEX execute: skip flash — midcap approved but SOL-only auto-exec | %s",
                opp.get("pair"),
            )
            return None

        flash_usdc = min(
            int(opp["size_usdc_micro"]),
            self.config["max_trade_usdc_micro"],
            CEX_DEX_CONFIG["flash_amount_usdc"],
        )
        flash_result = await build_and_execute_cex_dex_flash_arb(
            float(opp["cex_mid"]),
            float(opp["dex_mid"]),
            str(opp.get("direction") or ""),
            jupiter=self.executor,
            skip_ai=True,
            flash_usdc_micro=flash_usdc,
        )
        if flash_result is None:
            logger.info(
                "CEX-DEX execute: flash arb finished with no result (see flash-arb logs above) | pair=%s",
                opp.get("pair"),
            )
        return flash_result

    async def run(self) -> None:
        while True:
            try:
                opp = await self.detect_opportunity()
                if opp:
                    await self.execute(opp)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("CEX-DEX run cycle failed: %s", exc)
            await asyncio.sleep(max(0.8, CEX_POLL_INTERVAL))


async def create_cex_dex_bot(
    client: AsyncClient,
    keypair: Keypair,
    executor: JupiterExecutor,
) -> CexDexArbitrageBot:
    return CexDexArbitrageBot(client, keypair, executor)
