# liquidation_bot.py - Upgraded Kamino Liquidation Hunter (AI + Flash Loan + Jito)
import asyncio
import logging
import os
from typing import Any

import aiohttp
from solana.rpc.async_api import AsyncClient
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

import src.core.wallet as wallet_safety
from src.cex.price_feed import cex_feed
from src.core.circuit_breaker import circuit_breaker
from src.dex.jupiter import JupiterExecutor
from src.dex.kamino import KaminoFlashLoan  # Reuse your existing helper
from src.dex.kamino_api import KaminoAPI
from src.execution.jito import send_jito_bundle
from src.strategies.brain_signals import note_liquidation_best
from src.utils.ai import ai_agent_decide

logger = logging.getLogger(__name__)

MAIN_MARKET = Pubkey.from_string(
    "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF"
)  # Kamino Main USDC Market
MAIN_MARKET_STR = (
    os.getenv("KAMINO_LENDING_MARKET_PUBKEY")
    or os.getenv("KAMINO_LENDING_MARKET")
    or os.getenv("KAMINO_MARKET_PUBKEY")
    or str(MAIN_MARKET)
).strip()
# KLend debt reserve for USDC in MAIN_MARKET (set from Kamino UI/SDK — placeholder ix layout).
DEFAULT_USDC_DEBT_RESERVE = os.getenv("KAMINO_USDC_DEBT_RESERVE", "").strip()
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
METRICS_CONCURRENCY = int(os.getenv("KAMINO_METRICS_CONCURRENCY", "4"))
# Align lending market pubkey with GET /v2/kamino-market primary row (recommended).
KAMINO_SYNC_PRIMARY_MARKET = os.getenv("KAMINO_SYNC_PRIMARY_MARKET", "true").lower() == "true"
# Scan borrow-order feed across every listed market (more RPC/API load).
KAMINO_SCAN_ALL_MARKETS = os.getenv("KAMINO_SCAN_ALL_MARKETS", "false").lower() == "true"
KAMINO_METRICS_USE_STAKE_RATE = (
    os.getenv("KAMINO_METRICS_USE_STAKE_RATE", "false").lower() == "true"
)
# Alerts only when near liquidation (defaults are conservative monitoring thresholds).
def _alert_min_util() -> float:
    return float(os.getenv("KAMINO_ALERT_MIN_UTIL", "0.92"))


def _alert_min_ltv_ratio() -> float:
    return float(os.getenv("KAMINO_ALERT_MIN_LTV_RATIO", "0.92"))


class LiquidationBot:
    def __init__(self, client: AsyncClient, executor: JupiterExecutor, keypair):
        self.client = client
        self.executor = executor
        self.keypair = keypair
        self.kamino = KaminoFlashLoan(client, keypair)
        self.kamino_api = KaminoAPI()

    def _risk_from_stats(self, stats: dict[str, Any]) -> tuple[bool, float | None]:
        """
        Kamino metrics/refreshedStats heuristic (not on-chain HF).
        Returns (at_risk, display_hf) where display_hf is a loose 0..1 "distance from stress".
        """
        if not stats:
            return False, None
        try:
            util = float(stats.get("borrowUtilization") or 0)
            borrow = float(stats.get("userTotalBorrow") or 0)
            ltv = float(stats.get("loanToValue") or 0)
            liq = float(stats.get("liquidationLtv") or 0)
            if liq <= 0:
                liq = 0.75
        except (TypeError, ValueError):
            return False, None

        if borrow <= 1e-9:
            return False, None

        if util > 0:
            display_hf = max(0.0, 1.0 - util)
            return util >= _alert_min_util(), display_hf

        if ltv > 0:
            ratio = ltv / liq
            cushion = max(0.0, (liq - ltv) / liq)
            return ratio >= _alert_min_ltv_ratio(), cushion

        return False, None

    async def _fetch_latest_obligation_stats(
        self,
        session: aiohttp.ClientSession,
        lending_market_pk: str,
        obligation_pubkey: str,
    ) -> dict[str, Any] | None:
        url = self.kamino_api.obligation_metrics_history_url(
            lending_market_pk,
            obligation_pubkey,
            use_stake_rate_for_obligation=KAMINO_METRICS_USE_STAKE_RATE,
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                data = await KaminoAPI.safe_json(resp)
            if not isinstance(data, dict):
                return None
            history = data.get("history") or []
            if not history:
                return None
            last = history[-1]
            return last.get("refreshedStats") if isinstance(last, dict) else None
        except Exception as exc:
            logger.debug("Obligation metrics fetch failed %s: %s", obligation_pubkey[:8], exc)
            return None

    async def _enrich_obligation(
        self,
        session: aiohttp.ClientSession,
        sem: asyncio.Semaphore,
        row: dict[str, Any],
        lending_market_pk: str,
    ) -> dict | None:
        obligation = row.get("obligationPubkey") or row.get("obligation")
        owner = row.get("owner", "")
        if not obligation:
            return None
        debt_mint = row.get("debtLiquidityMint")
        try:
            debt_amount = int(row.get("remainingDebtAmount") or row.get("requestedDebtAmount") or 0)
        except (TypeError, ValueError):
            debt_amount = 0

        async with sem:
            stats = await self._fetch_latest_obligation_stats(
                session, lending_market_pk, obligation
            )
        at_risk, hf = self._risk_from_stats(stats or {})
        if not at_risk or hf is None:
            return None

        deposits = row.get("deposits") or []
        collateral_reserve = ""
        if deposits and isinstance(deposits[0], dict):
            collateral_reserve = str(deposits[0].get("depositReserve") or "")

        normalized = {
            "obligation": obligation,
            "owner": owner,
            "lending_market": lending_market_pk,
            "health_factor": hf,
            "debt_mint": debt_mint,
            "debt_amount": debt_amount,
            "collateral_value": float(row.get("collateralValue") or 0),
            "collateral_reserve": collateral_reserve,
            "debt_reserve": DEFAULT_USDC_DEBT_RESERVE if debt_mint == USDC_MINT else "",
        }
        normalized["profit_potential_usdc"] = self._estimate_liquidation_profit(normalized)
        return normalized

    async def fetch_liquidatable_positions(self, limit: int = 50) -> list[dict]:
        """
        Load obligations from Kamino Buildkit API (v2), then score with obligation metrics.

        Endpoints (see kamino_api.py):
          GET /{v2}/kamino-market/{lendingMarket}/obligations-with-open-borrow-orders
          GET /{v2}/kamino-market/{lendingMarket}/obligations/{obligation}/metrics/history
        Optional: GET /{v2}/kamino-market to sync primary market or scan all markets.
        """
        try:
            async with aiohttp.ClientSession() as session:
                markets_to_scan: list[str] = []
                if KAMINO_SCAN_ALL_MARKETS:
                    market_rows = await self.kamino_api.fetch_markets(session)
                    markets_to_scan = [
                        str(m["lendingMarket"]) for m in market_rows if m.get("lendingMarket")
                    ]
                    if not markets_to_scan:
                        logger.warning("Kamino: /v2/kamino-market returned no markets.")
                        return []
                    logger.debug(
                        "Kamino multi-market scan: %s markets (limit=%s)",
                        len(markets_to_scan),
                        limit,
                    )
                else:
                    effective = MAIN_MARKET_STR
                    if KAMINO_SYNC_PRIMARY_MARKET:
                        market_rows = await self.kamino_api.fetch_markets(session)
                        primary = self.kamino_api.primary_lending_market(market_rows)
                        if primary:
                            effective = primary
                            logger.debug("Kamino primary market synced: %s", effective[:8])
                    markets_to_scan = [effective]

                raw_rows: list[tuple[str, dict[str, Any]]] = []
                per_market = max(1, limit // max(len(markets_to_scan), 1))

                for mk in markets_to_scan:
                    list_url = self.kamino_api.obligations_open_borrow_orders_url(mk)
                    async with session.get(
                        list_url, timeout=aiohttp.ClientTimeout(total=25)
                    ) as resp:
                        data = await KaminoAPI.safe_json(resp)
                    if not isinstance(data, dict):
                        continue
                    obligations = data.get("obligations") or []
                    for row in obligations[:per_market]:
                        raw_rows.append((mk, row))

                raw_rows = raw_rows[:limit]
                if not raw_rows:
                    logger.info("Kamino: no obligations in borrow-order feed this cycle.")
                    return []

                sem = asyncio.Semaphore(METRICS_CONCURRENCY)
                enriched = await asyncio.gather(
                    *[
                        self._enrich_obligation(session, sem, row, lending_mk)
                        for lending_mk, row in raw_rows
                    ],
                    return_exceptions=True,
                )
                out: list[dict] = []
                for item in enriched:
                    if isinstance(item, dict) and item:
                        out.append(item)
                    elif isinstance(item, Exception):
                        logger.debug("Enrich failed: %s", item)
                return out
        except Exception as e:
            logger.error("Failed to fetch liquidations: %s", e)
            return []

    def _estimate_liquidation_profit(self, position: dict) -> float:
        from src.strategies.liquidation_executor import get_liquidation_executor

        return get_liquidation_executor()._estimate_liquidation_profit(position)

    async def build_liquidation_tx(self, position: dict, flash_amount: int):
        """Flash Loan + Liquidate + Repay + Take Bonus"""
        obligation_pk = Pubkey.from_string(position["obligation"])
        debt_reserve_str = (position.get("debt_reserve") or "").strip()
        collateral_reserve_str = (position.get("collateral_reserve") or "").strip()
        if not debt_reserve_str or not collateral_reserve_str:
            raise ValueError(
                "Missing debt_reserve or collateral_reserve for liquidation ix "
                "(set KAMINO_USDC_DEBT_RESERVE and ensure obligation row has deposits[].depositReserve)."
            )

        debt_reserve = Pubkey.from_string(debt_reserve_str)
        collateral_reserve = Pubkey.from_string(collateral_reserve_str)
        debt_amt = int(position.get("debt_amount") or 0)
        lending_market = Pubkey.from_string(
            (position.get("lending_market") or MAIN_MARKET_STR).strip()
        )
        repay_mint = str(position.get("debt_mint") or USDC_MINT)
        from src.strategies.liquidation_executor import get_liquidation_executor

        partial_pct = float(os.getenv("LIQUIDATION_PARTIAL_PCT", "0.5"))
        liquidity_amount = get_liquidation_executor().partial_liquidity_amount(
            debt_amt,
            flash_amount,
            partial_pct=partial_pct,
        )
        slip_bps = int(os.getenv("LIQUIDATION_SLIPPAGE_BPS", "200"))

        # 1. Flash borrow USDC
        borrow_ix = await self.kamino.get_flash_borrow_ix(USDC_MINT, flash_amount, obligation_pk)

        # 2. KLend liquidate + redeem collateral
        liquidate_ix = await self.kamino.get_liquidation_ix(
            obligation_pk,
            debt_reserve,
            collateral_reserve,
            debt_amt,
            lending_market=lending_market,
            repay_mint=repay_mint,
            liquidity_amount=liquidity_amount,
            market_pubkey=str(position.get("lending_market") or MAIN_MARKET_STR),
            slippage_bps=slip_bps,
        )

        # 3. Jupiter swap collateral bonus → USDC if needed
        # 4. Flash repay (principal + Klend flash fee)
        flash_fee_bps = int(os.getenv("KAMINO_FLASH_LOAN_FEE_BPS", "5"))
        repay_total = flash_amount + max(1, (flash_amount * flash_fee_bps) // 10_000)
        repay_ix = await self.kamino.get_flash_repay_ix(
            USDC_MINT,
            repay_total,
            obligation_pk,
            borrow_instruction_index=0,
        )

        all_ixs = [borrow_ix, liquidate_ix, repay_ix]

        # Build Versioned Tx with ALTs
        recent = (await self.client.get_latest_blockhash()).value.blockhash
        msg = MessageV0.try_compile(
            payer=self.keypair.pubkey(),
            instructions=all_ixs,
            address_lookup_table_accounts=[],  # Add from Jupiter if swapping
            recent_blockhash=recent,
        )
        return VersionedTransaction(msg, [self.keypair])

    async def monitor_liquidations(self):
        """Run one liquidation scan cycle (called from main.py loop)."""
        if circuit_breaker.should_pause():
            logger.debug("Liquidation monitor skipped (circuit breaker)")
            return

        positions = await self.fetch_liquidatable_positions(limit=30)

        if positions:
            top = max(positions, key=lambda p: float(p.get("profit_potential_usdc") or 0.0))
            note_liquidation_best(
                {
                    "profit_usdc": float(top.get("profit_potential_usdc") or 0.0),
                    "obligation": str(top.get("obligation") or "")[:12],
                    "health_factor": top.get("health_factor"),
                }
            )

        try:
            liq_min_ai = int(os.getenv("LIQUIDATION_AI_MIN_CONFIDENCE", "72"))
        except (TypeError, ValueError):
            liq_min_ai = 72
        liq_min_ai = max(0, min(100, liq_min_ai))

        min_profit = float(os.getenv("LIQUIDATION_MIN_PROFIT_USDC", "3.5"))
        for pos in positions:
            profit = pos["profit_potential_usdc"]
            if profit < min_profit:
                continue

            opportunity = {
                "type": "liquidation",
                "health_factor": pos["health_factor"],
                "profit_usdc": profit,
                "obligation": pos["obligation"][:8] + "...",
            }
            opportunity["cex_prices"] = await cex_feed.get_multiple_prices(["SOL/USDC"])

            balance = (await self.client.get_balance(self.keypair.pubkey())).value
            decision = await ai_agent_decide(opportunity, balance)

            td = decision.get("trade_decision") or {}
            try:
                conf = int(td.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0

            if decision.get("final_action") == "APPROVE" and conf > liq_min_ai:
                logger.warning(
                    "🚨 LIQUIDATION OPPORTUNITY! Profit ~$%.2f on %s",
                    profit,
                    pos["obligation"][:8],
                )

                flash_amt = int(profit * 1_000_000 * 8)
                tx = await self.build_liquidation_tx(pos, flash_amount=flash_amt)

                sim = await self.client.simulate_transaction(tx)
                if sim.value.err is None:
                    wallet_safety.record_successful_simulation()
                    test_mode = os.getenv("TEST_MODE", "true").lower() == "true"
                    if test_mode:
                        logger.info("TEST_MODE=true — skip liquidation bundle send")
                        continue
                    ok_w, wreason = wallet_safety.before_live_send(flash_amt)
                    if not ok_w:
                        logger.warning("Liquidation blocked by wallet safety: %s", wreason)
                        continue
                    bundle_result = await send_jito_bundle([tx])
                    logger.info("Liquidation bundle sent: %s", bundle_result)
                    wallet_safety.record_live_trade_usdc_micro(flash_amt)
                else:
                    logger.error("Simulation failed for liquidation")
