# collateral_swap_executor.py — Kamino rate-spread scan + Jupiter/Kamino collateral swap tx build
import logging
import os
from typing import Any

import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.keypair import Keypair

import src.core.wallet as wallet_safety
from src.cex.price_feed import cex_feed
from src.core.circuit_breaker import circuit_breaker
from src.dex.jupiter import JupiterExecutor
from src.utils.ai import ai_agent_decide

logger = logging.getLogger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

# Kamino Main Market (Klend); matches kamino_helper.KAMINO_MAIN_MARKET
DEFAULT_KAMINO_LENDING_MARKET = "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF"
KAMINO_METRICS_URL_TMPL = "https://api.kamino.finance/kamino-market/{market}/reserves/metrics"


class CollateralSwapExecutor:
    def __init__(
        self,
        client: AsyncClient,
        keypair: Keypair,
        jupiter: JupiterExecutor | None = None,
    ):
        self.client = client
        self.keypair = keypair
        self.executor = jupiter or JupiterExecutor()

    @staticmethod
    def _borrow_rates_from_env() -> dict[str, dict[str, Any]]:
        """When Kamino HTTP markets payload is unavailable, optional APY decimals e.g. 0.08 = 8%."""
        out: dict[str, dict[str, Any]] = {}
        u = os.getenv("COLLATERAL_FALLBACK_USDC_BORROW_APY_DECIMAL", "").strip()
        s = os.getenv("COLLATERAL_FALLBACK_SOL_BORROW_APY_DECIMAL", "").strip()
        if u:
            try:
                out[USDC_MINT] = {
                    "borrow_apy": float(u),
                    "supply_apy": 0.0,
                    "utilization": 0.0,
                }
            except ValueError:
                pass
        if s:
            try:
                out[SOL_MINT] = {
                    "borrow_apy": float(s),
                    "supply_apy": 0.0,
                    "utilization": 0.0,
                }
            except ValueError:
                pass
        return out

    @staticmethod
    def _metrics_row_to_rate(row: dict[str, Any]) -> dict[str, Any] | None:
        mint = row.get("liquidityTokenMint")
        if not mint:
            return None
        try:
            borrow = float(row.get("borrowApy") or 0.0)
            supply = float(row.get("supplyApy") or 0.0)
        except (TypeError, ValueError):
            return None
        util = 0.0
        try:
            tb = float(row.get("totalBorrow") or 0.0)
            ts = float(row.get("totalSupply") or 0.0)
            if ts > 0:
                util = min(1.0, max(0.0, tb / ts))
        except (TypeError, ValueError):
            pass
        return {
            "borrow_apy": borrow,
            "supply_apy": supply,
            "utilization": util,
            "liquidity_token": row.get("liquidityToken"),
            "reserve_pubkey": row.get("reserve"),
        }

    async def get_kamino_borrow_rates(self) -> dict[str, dict[str, Any]]:
        """
        Fetch reserve borrow/supply APY from Kamino public metrics API.

        Response ``borrowApy`` / ``supplyApy`` are **annual decimals** (e.g. 0.053 ≈ 5.3%),
        consistent with ``find_rate_arbitrage_opportunity`` spread_bps = (sol - usdc) * 10_000.
        """
        market = (
            os.getenv("KAMINO_LENDING_MARKET_PUBKEY") or DEFAULT_KAMINO_LENDING_MARKET
        ).strip()
        url = KAMINO_METRICS_URL_TMPL.format(market=market)
        rates: dict[str, dict[str, Any]] = {}
        headers = {"User-Agent": "solana-arb-bot/1.0", "Accept": "application/json"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Kamino reserve metrics HTTP %s for market=%s", resp.status, market[:8]
                        )
                    else:
                        data = await resp.json()
                        if not isinstance(data, list):
                            logger.warning(
                                "Kamino reserve metrics: expected list, got %s", type(data).__name__
                            )
                        else:
                            for row in data:
                                if not isinstance(row, dict):
                                    continue
                                parsed = self._metrics_row_to_rate(row)
                                if not parsed:
                                    continue
                                mint_key = str(row.get("liquidityTokenMint") or "")
                                rates[mint_key] = {
                                    "borrow_apy": parsed["borrow_apy"],
                                    "supply_apy": parsed["supply_apy"],
                                    "utilization": parsed["utilization"],
                                }
                            logger.debug(
                                "Kamino metrics: market=%s reserves=%s",
                                market[:8],
                                len(data),
                            )
        except Exception as exc:
            reason = str(exc).strip() or repr(exc).strip() or exc.__class__.__name__
            logger.warning("Kamino borrow rates fetch failed: %s", reason)

        fb = self._borrow_rates_from_env()
        for mint_key, row in fb.items():
            rates.setdefault(mint_key, row)

        usdc_rate = float(rates.get(USDC_MINT, {}).get("borrow_apy") or 0.0)
        sol_rate = float(rates.get(SOL_MINT, {}).get("borrow_apy") or 0.0)
        logger.info("Kamino rates: USDC=%s SOL=%s", f"{usdc_rate:.2%}", f"{sol_rate:.2%}")
        return rates

    async def find_rate_arbitrage_opportunity(
        self, min_spread_bps: int = 150
    ) -> list[dict[str, Any]]:
        """Scan for borrow-rate spread between USDC and SOL reserves (illustrative heuristic)."""
        rates = await self.get_kamino_borrow_rates()
        opportunities: list[dict[str, Any]] = []

        usdc_rate = rates.get(USDC_MINT, {}).get("borrow_apy", 0.0)
        sol_rate = rates.get(SOL_MINT, {}).get("borrow_apy", 0.0)

        try:
            spread_bps = (float(sol_rate) - float(usdc_rate)) * 10_000.0
        except (TypeError, ValueError):
            return opportunities

        if spread_bps > min_spread_bps:
            opportunities.append(
                {
                    "type": "collateral_swap",
                    "borrow_low": "USDC",
                    "repay_high": "SOL",
                    "borrow_mint": USDC_MINT,
                    "target_mint": SOL_MINT,
                    "spread_bps": spread_bps,
                    "low_rate": usdc_rate,
                    "high_rate": sol_rate,
                }
            )

        return opportunities

    async def execute_collateral_swap(self, opportunity: dict, flash_amount: int):
        """
        AI gate → build collateral swap tx (flash borrow → Jupiter swap → repay + fee) → simulate → send.
        Respects TEST_MODE (no chain send).
        """
        borrow_mint = str(opportunity.get("borrow_mint") or USDC_MINT)
        target_mint = str(opportunity.get("target_mint") or SOL_MINT)

        logger.info(
            "Collateral swap candidate | spread≈%.2f bps | borrow=%s → target=%s | flash=%s",
            float(opportunity.get("spread_bps") or 0),
            borrow_mint[:8],
            target_mint[:8],
            flash_amount,
        )

        if circuit_breaker.should_pause():
            logger.info("Collateral swap skipped (circuit breaker)")
            return None

        balance = (await self.client.get_balance(self.keypair.pubkey())).value
        payload = {
            "strategy": "collateral_swap_rate_arb",
            **opportunity,
            "flash_amount": flash_amount,
        }
        payload["cex_prices"] = await cex_feed.get_multiple_prices(["SOL/USDC"])
        decision = await ai_agent_decide(payload, balance)
        if decision.get("final_action") != "APPROVE":
            td = decision.get("trade_decision") or {}
            logger.info("[AI REJECT] collateral swap: %s", td.get("reasoning", ""))
            return None

        test_mode = os.getenv("TEST_MODE", "true").lower() == "true"
        if test_mode:
            logger.info(
                "TEST MODE: would execute collateral swap borrow=%s target=%s lamports=%s",
                borrow_mint[:8],
                target_mint[:8],
                flash_amount,
            )
            return None

        tx = await self.executor.build_collateral_swap_tx(
            borrow_reserve_mint=borrow_mint,
            target_collateral_mint=target_mint,
            flash_amount=flash_amount,
        )

        sim = await self.client.simulate_transaction(tx)
        if sim.value.err is not None:
            logger.error("Collateral swap simulation failed: %s", sim.value.err)
            return None

        wallet_safety.record_successful_simulation()

        ok_w, wreason = wallet_safety.before_live_send(flash_amount)
        if not ok_w:
            logger.warning("Collateral swap blocked by wallet safety: %s", wreason)
            return None

        result = await self.client.send_raw_transaction(
            bytes(tx.serialize()),
            opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3),
        )
        sig = result.value
        logger.warning("Collateral swap sent https://solscan.io/tx/%s", sig)
        wallet_safety.record_live_trade_usdc_micro(flash_amount)
        return sig
