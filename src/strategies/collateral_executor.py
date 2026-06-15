#!/usr/bin/env python3
"""
Real Collateral Rate Arbitrage Executor — cross-market Kamino carry + flash swap.
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

from src.config.settings import Settings, get_settings
from src.dex.jupiter import JupiterExecutor
from src.dex.kamino_api import KaminoAPI
from src.strategies.collateral_swap import (
    DEFAULT_KAMINO_LENDING_MARKET,
    KAMINO_METRICS_URL_TMPL,
)
from src.v2.attempt_log import append_attempt

logger = logging.getLogger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

_executor: CollateralExecutor | None = None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _normalize_apy(val: Any) -> float:
    """Kamino metrics: annual decimal (0.05) or occasional percent-scale (5.0)."""
    try:
        v = float(val or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if abs(v) > 3.0:
        return v / 100.0
    return v


class CollateralExecutor:
    """Scan Kamino markets for net supply−borrow carry; execute flash borrow → swap → repay."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._api = KaminoAPI()
        self._last_block_reason = ""

    def _min_net_bps(self) -> float:
        raw = os.getenv("COLLATERAL_MIN_NET_BPS", "").strip()
        if raw:
            return float(raw)
        return _env_float(
            "COLLATERAL_MIN_SPREAD_BPS",
            float(getattr(self.settings, "COLLATERAL_MIN_SPREAD_BPS", 35)),
        )

    def _flash_amount_micro(self) -> int:
        cap = _env_int(
            "COLLATERAL_FLASH_AMOUNT_USDC_MICRO",
            int(getattr(self.settings, "COLLATERAL_FLASH_AMOUNT_USDC_MICRO", 35_000_000)),
        )
        tier_cap = _env_int("COLLATERAL_MAX_TRADE_USDC_MICRO", 35_000_000)
        return min(cap, tier_cap)

    def _max_plausible_carry_bps(self) -> float:
        return _env_float("COLLATERAL_MAX_PLAUSIBLE_CARRY_BPS", 500.0)

    def _flash_fee_bps(self) -> float:
        return _env_float("COLLATERAL_FLASH_FEE_BPS", 5.0)

    def _swap_slip_bps(self) -> float:
        return _env_float("COLLATERAL_SWAP_SLIPPAGE_BPS", 8.0)

    def _jito_tip_bps(self) -> float:
        return _env_float("COLLATERAL_JITO_TIP_BPS", 8.0)

    def _allowed_borrow_mints(self) -> set[str]:
        raw = (os.getenv("COLLATERAL_ALLOWED_BORROW_MINTS") or USDC_MINT).strip()
        return {part.strip() for part in raw.split(",") if part.strip()}

    def _collateral_ai_min_confidence(self) -> int:
        raw = (os.getenv("COLLATERAL_AI_MIN_CONFIDENCE") or "").strip()
        if raw:
            return _env_int("COLLATERAL_AI_MIN_CONFIDENCE", 55)
        try:
            return int(getattr(self.settings, "COLLATERAL_AI_MIN_CONFIDENCE", 55))
        except (TypeError, ValueError, AttributeError):
            return 55

    async def _get_ai_confidence(
        self, payload: dict[str, Any], sol_lamports: int
    ) -> tuple[int, dict[str, Any]]:
        from src.utils.ai import ai_agent_decide

        ai_min = self._collateral_ai_min_confidence()
        decision = await ai_agent_decide(payload, sol_lamports, min_confidence=ai_min)
        trade = decision.get("trade_decision") or {}
        try:
            confidence = int(trade.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        return confidence, decision

    async def _fetch_kamino_markets(self) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        headers = {"User-Agent": "solana-arb-bot/1.0", "Accept": "application/json"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                rows = await self._api.fetch_markets(session)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                pk = row.get("lendingMarket") or row.get("pubkey")
                if pk:
                    markets.append({"pubkey": str(pk), "name": row.get("name"), "raw": row})
        except Exception as exc:
            logger.warning("Kamino markets fetch failed: %s", exc)

        default_pk = (
            os.getenv("KAMINO_LENDING_MARKET_PUBKEY") or DEFAULT_KAMINO_LENDING_MARKET
        ).strip()
        if default_pk and not any(m["pubkey"] == default_pk for m in markets):
            markets.insert(0, {"pubkey": default_pk, "name": "default"})

        extra = [
            s.strip()
            for s in (os.getenv("KAMINO_EXTRA_MARKET_PUBKEYS") or "").split(",")
            if s.strip()
        ]
        for pk in extra:
            if not any(m["pubkey"] == pk for m in markets):
                markets.append({"pubkey": pk, "name": "extra"})

        return markets

    async def _fetch_market_reserves(self, market_pubkey: str) -> list[dict[str, Any]]:
        url = KAMINO_METRICS_URL_TMPL.format(market=market_pubkey)
        headers = {"User-Agent": "solana-arb-bot/1.0", "Accept": "application/json"}
        reserves: list[dict[str, Any]] = []
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status != 200:
                        return reserves
                    data = await resp.json()
            if not isinstance(data, list):
                return reserves
            for row in data:
                if not isinstance(row, dict):
                    continue
                mint = row.get("liquidityTokenMint")
                if not mint:
                    continue
                borrow_apy = _normalize_apy(row.get("borrowApy"))
                supply_apy = _normalize_apy(row.get("supplyApy"))
                try:
                    supply_usd = float(row.get("totalSupplyUsd") or 0.0)
                except (TypeError, ValueError):
                    supply_usd = 0.0
                reserves.append(
                    {
                        "mint": str(mint),
                        "symbol": row.get("liquidityToken") or str(mint)[:8],
                        "borrow_apy": borrow_apy,
                        "supply_apy": supply_apy,
                        "borrow_apr": borrow_apy,
                        "supply_apr": supply_apy,
                        "reserve_pubkey": row.get("reserve"),
                        "supply_liquidity_usd": supply_usd,
                    }
                )
        except Exception as exc:
            logger.debug("Kamino reserves %s failed: %s", market_pubkey[:8], exc)
        return reserves

    def _calculate_net_carry(
        self,
        borrow: dict[str, Any],
        supply: dict[str, Any],
        *,
        size_usdc_micro: int | None = None,
    ) -> dict[str, float]:
        """Net annualized carry after flash fee, swap slippage, and Jito tip drag."""
        borrow_apr = _normalize_apy(
            borrow.get("borrow_apr") or borrow.get("borrow_apy") or borrow.get("apr")
        )
        supply_apr = _normalize_apy(
            supply.get("supply_apr") or supply.get("supply_apy") or supply.get("apr")
        )
        flash_fee_bps = self._flash_fee_bps()
        swap_slip_bps = self._swap_slip_bps()
        jito_tip_bps = self._jito_tip_bps()

        gross_bps = (supply_apr - borrow_apr) * 10_000.0
        drag_bps = flash_fee_bps + swap_slip_bps + jito_tip_bps
        net_bps = gross_bps - drag_bps

        size_micro = size_usdc_micro or self._flash_amount_micro()
        profit_usd = (max(0.0, net_bps) / 10_000.0) * (size_micro / 1_000_000.0)

        return {
            "gross_bps": gross_bps,
            "net_bps": max(0.0, net_bps),
            "profit_usd": profit_usd,
        }

    async def find_opportunity(self) -> list[dict[str, Any]]:
        """Scan Kamino markets for profitable cross-reserve carry."""
        return await self.find_opportunities()

    async def find_opportunities(self) -> list[dict[str, Any]]:
        min_net = self._min_net_bps()
        size_micro = self._flash_amount_micro()
        max_carry = self._max_plausible_carry_bps()
        opportunities: list[dict[str, Any]] = []

        markets = await self._fetch_kamino_markets()
        for market in markets:
            market_pk = market["pubkey"]
            reserves = await self._fetch_market_reserves(market_pk)
            if len(reserves) < 2:
                continue

            min_liq_usd = _env_float("COLLATERAL_MIN_RESERVE_LIQUIDITY_USD", 25.0)
            allowed_borrow = self._allowed_borrow_mints()
            for i, borrow_reserve in enumerate(reserves):
                if str(borrow_reserve.get("mint") or "") not in allowed_borrow:
                    continue
                borrow_liq = float(borrow_reserve.get("supply_liquidity_usd") or 0.0)
                if borrow_liq < min_liq_usd:
                    continue
                for supply_reserve in reserves[i + 1 :]:
                    if borrow_reserve.get("symbol") == supply_reserve.get("symbol"):
                        continue
                    carry = self._calculate_net_carry(
                        borrow_reserve, supply_reserve, size_usdc_micro=size_micro
                    )
                    gross_bps = float(carry["gross_bps"])
                    net_bps = float(carry["net_bps"])
                    if gross_bps > max_carry or net_bps <= min_net:
                        continue
                    opportunities.append(
                        {
                            "type": "collateral_swap",
                            "market": market_pk,
                            "market_name": market.get("name"),
                            "borrow_reserve": borrow_reserve.get("reserve_pubkey"),
                            "supply_reserve": supply_reserve.get("reserve_pubkey"),
                            "borrow_mint": borrow_reserve["mint"],
                            "target_mint": supply_reserve["mint"],
                            "borrow_low": borrow_reserve.get("symbol"),
                            "repay_high": supply_reserve.get("symbol"),
                            "borrow_apy": borrow_reserve["borrow_apy"],
                            "supply_apy": supply_reserve["supply_apy"],
                            "gross_bps": round(gross_bps, 3),
                            "net_bps": round(net_bps, 3),
                            "spread_bps": round(net_bps, 3),
                            "profit_usd": round(float(carry["profit_usd"]), 4),
                            "size_usdc_micro": size_micro,
                            "active": True,
                        }
                    )

        opportunities.sort(key=lambda o: float(o.get("net_bps") or 0), reverse=True)
        if opportunities:
            top = opportunities[0]
            logger.info(
                "Collateral carry | market=%s net_bps=%.1f gross=%.1f borrow=%s supply=%s",
                str(top.get("market", ""))[:8],
                float(top.get("net_bps") or 0),
                float(top.get("gross_bps") or 0),
                top.get("borrow_low"),
                top.get("repay_high"),
            )
        return opportunities

    async def _borrow_mint_ata_micro(
        self, client: Any, owner_pubkey: Any, mint: str
    ) -> int:
        from solana.rpc.async_api import AsyncClient
        from solders.pubkey import Pubkey
        from spl.token.instructions import get_associated_token_address

        from src.core.rpc_config import call_with_rpc_fallback, is_rpc_degraded_error

        ata = get_associated_token_address(owner_pubkey, Pubkey.from_string(mint))

        async def _fetch(rpc_url: str) -> int:
            async with AsyncClient(rpc_url) as rpc_client:
                resp = await rpc_client.get_token_account_balance(ata)
            return int(resp.value.amount)

        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return await call_with_rpc_fallback(
                    "balance", _fetch, label="borrow_ata_balance"
                )
            except Exception as exc:
                last_exc = exc
                if is_rpc_degraded_error(exc) or "429" in str(exc).lower():
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                break

        if client is not None:
            try:
                resp = await client.get_token_account_balance(ata)
                return int(resp.value.amount)
            except Exception:
                pass
        if last_exc:
            logger.debug("borrow ATA balance failed for %s: %s", mint[:8], last_exc)
        return 0

    def _flash_repay_buffer_micro(self, flash_micro: int) -> int:
        fee_bps = self._flash_fee_bps()
        return max(10_000, (flash_micro * int(fee_bps * 100)) // 10_000 + 1)

    async def _safe_get_balance(self, mint: str, owner_pubkey: Any | None = None) -> int:
        """Multi-RPC fallback for token balances by mint."""
        from solders.pubkey import Pubkey

        from src.core.wallet import get_wallet_pubkey

        if owner_pubkey is None:
            pk = (
                get_wallet_pubkey()
                or getattr(self.settings, "wallet_pubkey", None)
                or getattr(self.settings, "WALLET_PUBKEY", None)
            )
            if not pk:
                return 0
            owner_pubkey = Pubkey.from_string(str(pk))
        return await self._borrow_mint_ata_micro(None, owner_pubkey, mint)

    async def _safe_get_usdc_micro(self) -> int:
        """RPC-resilient on-chain USDC balance (micro units)."""
        from src.core.rpc_config import is_rpc_degraded_error
        from src.core.wallet import get_usdc_balance_robust, get_wallet_pubkey

        pubkey = (
            get_wallet_pubkey()
            or getattr(self.settings, "wallet_pubkey", None)
            or getattr(self.settings, "WALLET_PUBKEY", None)
            or ""
        )
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                usd = await get_usdc_balance_robust(str(pubkey) if pubkey else None)
                return int(max(0.0, usd) * 1_000_000)
            except Exception as exc:
                last_exc = exc
                if is_rpc_degraded_error(exc) or "429" in str(exc).lower():
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                logger.warning("USDC balance error: %s", exc)
                break
        if last_exc:
            logger.warning("USDC balance retries exhausted: %s", last_exc)
        return 0

    async def _safe_get_sol_lamports(self, keypair: Any) -> int:
        """RPC-resilient SOL lamports for AI gate (429 backoff + stale cache)."""
        from src.core.rpc_config import get_robust_sol_balance

        sol = await get_robust_sol_balance(str(keypair.pubkey()))
        return int(max(0.0, sol) * 1_000_000_000)

    def _calculate_dynamic_tip(self, opp: dict[str, Any]) -> int:
        from src.dex.jupiter import resolve_execution_jito_tip_lamports

        net_bps = float(opp.get("net_bps") or 0)
        gross_bps = float(opp.get("gross_bps") or net_bps)
        size_micro = int(opp.get("size_usdc_micro") or self._flash_amount_micro())
        profit_usd = float(opp.get("profit_usd") or 0)
        override = profit_usd if profit_usd > 0 else None
        env_tip = os.getenv("COLLATERAL_JITO_TIP_LAMPORTS", "").strip()
        if env_tip:
            return int(env_tip)
        return int(
            resolve_execution_jito_tip_lamports(
                net_bps,
                size_usdc_micro=size_micro,
                gross_bps=gross_bps,
                override_net_usd=override,
            )
        )

    def _write_exception_log(self, opp: dict[str, Any], exc: Exception) -> None:
        path = Path(os.getenv("MEV_EXCEPTIONS_LOG", "logs/mev_exceptions.log"))
        path.parent.mkdir(parents=True, exist_ok=True)
        market = str(opp.get("market") or "")[:12]
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"{datetime.now(UTC).isoformat()} | market={market} | "
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}\n"
            )

    async def execute(self, opp: dict[str, Any]) -> bool:
        """Hardened flash borrow → Jupiter swap → repay via Kamino + Jito bundle."""
        import src.core.wallet as wallet_safety
        from solana.rpc.async_api import AsyncClient

        from src.cex.price_feed import cex_feed
        from src.core.circuit_breaker import circuit_breaker
        from src.core.rpc_config import call_with_rpc_fallback
        from src.core.rpc_urls import resolve_rpc_url
        from src.dex.kamino import KaminoFlashLoan
        from src.execution.jito_bundle import get_jito_bundle_executor
        borrow_mint = str(opp.get("borrow_mint") or "")
        target_mint = str(opp.get("target_mint") or "")
        flash_micro = int(opp.get("size_usdc_micro") or self._flash_amount_micro())
        net_bps = float(opp.get("net_bps") or 0)
        market_label = str(opp.get("market") or "")[:8]

        if net_bps < self._min_net_bps() or not borrow_mint or not target_mint:
            self._log_attempt(opp, "below_threshold")
            return False

        if circuit_breaker.should_pause():
            logger.info("Collateral execute skipped (circuit breaker)")
            self._log_attempt(opp, "circuit_breaker")
            return False

        client: AsyncClient | None = None
        try:
            logger.info(
                "Collateral execute: market=%s net_bps=%.1f profit=$%.2f",
                market_label,
                net_bps,
                float(opp.get("profit_usd") or 0),
            )

            usdc_micro = await self._safe_get_usdc_micro()
            min_usdc_micro = int(
                float(os.getenv("V2_MIN_USDC_BALANCE", "15")) * 1_000_000
            )
            if usdc_micro < min_usdc_micro:
                logger.warning(
                    "Low USDC balance ($%.2f), skipping collateral execute (min $%.2f)",
                    usdc_micro / 1_000_000.0,
                    min_usdc_micro / 1_000_000.0,
                )
                self._log_attempt(opp, "low_usdc")
                return False

            jupiter = JupiterExecutor(settings=self.settings)
            kp = jupiter.keypair
            if kp is None:
                logger.warning("Collateral execute: no signing keypair")
                self._log_attempt(opp, "no_signer")
                return False

            rpc = resolve_rpc_url("default")
            client = AsyncClient(rpc)
            kamino = KaminoFlashLoan(client, kp)
            sol_lamports = await self._safe_get_sol_lamports(kp)

            payload = {
                "strategy": "collateral_swap_rate_arb",
                **opp,
                "flash_amount": flash_micro,
            }
            payload["cex_prices"] = await cex_feed.get_multiple_prices(["SOL/USDC"])
            effective_min = self._collateral_ai_min_confidence()
            heuristic_min = _env_float("COLLATERAL_HEURISTIC_AUTO_APPROVE_NET_BPS", 300.0)

            if _env_bool("ENABLE_COLLATERAL_RATE_AI", True):
                try:
                    if net_bps > 120:
                        from src.core.ai_decision import enhanced_ai_approve

                        approved, ai_conf = await enhanced_ai_approve(
                            payload, min_conf=effective_min
                        )
                        if not approved and net_bps < heuristic_min:
                            logger.info(
                                "AI reject | conf=%s < %d | carry=%.1f bps",
                                ai_conf,
                                effective_min,
                                net_bps,
                            )
                            self._log_attempt(opp, "ai_reject")
                            return False
                        logger.info(
                            "AI %s | conf=%s | strong carry=%.1f bps",
                            "APPROVED" if approved else "OVERRIDDEN",
                            ai_conf,
                            net_bps,
                        )
                    else:
                        ai_conf, _decision = await self._get_ai_confidence(
                            payload, sol_lamports
                        )
                    if net_bps <= 120 and ai_conf < effective_min:
                        if net_bps >= heuristic_min:
                            logger.info(
                                "AI reject OVERRIDDEN for STRONG collateral carry "
                                "(%.1f bps >= %d)",
                                net_bps,
                                int(heuristic_min),
                            )
                        else:
                            logger.info(
                                "AI reject | conf=%s < %d | carry=%.1f bps",
                                ai_conf,
                                effective_min,
                                net_bps,
                            )
                            self._log_attempt(opp, "ai_reject")
                            return False
                    elif net_bps <= 120:
                        logger.info("AI APPROVED | conf=%s", ai_conf)
                except Exception as exc:
                    logger.warning(
                        "AI decision failed, falling back to heuristic: %s", exc
                    )
                    if net_bps < heuristic_min:
                        self._log_attempt(opp, "ai_reject")
                        return False
            else:
                logger.info("Collateral AI gate disabled — carry threshold only")

            if _env_bool("TEST_MODE", self.settings.test_mode):
                logger.info(
                    "TEST_MODE — skip live collateral | borrow=%s target=%s micro=%s",
                    borrow_mint[:8],
                    target_mint[:8],
                    flash_micro,
                )
                self._log_attempt(opp, "test_mode")
                return False

            slippage = int(
                os.getenv(
                    "COLLATERAL_EXECUTION_SLIPPAGE_BPS",
                    os.getenv(
                        "COLLATERAL_SWAP_SLIPPAGE_BPS",
                        str(getattr(self.settings, "MAX_SLIPPAGE_BPS", 60)),
                    ),
                )
            )
            repay_buffer = self._flash_repay_buffer_micro(flash_micro)
            pre_borrow_ata = await self._safe_get_balance(borrow_mint, kp.pubkey())
            max_swap = pre_borrow_ata + flash_micro - flash_micro - repay_buffer
            swap_micro = max(0, max_swap)
            min_swap = _env_int("COLLATERAL_MIN_SWAP_USDC_MICRO", 500_000)
            if swap_micro < min_swap:
                logger.info(
                    "Collateral skip: need %s micro %s in ATA for flash repay (have %s pre-borrow)",
                    flash_micro + repay_buffer,
                    borrow_mint[:8],
                    pre_borrow_ata,
                )
                self._log_attempt(opp, "insufficient_borrow_mint_for_repay")
                return False

            tx = await kamino.build_collateral_swap_tx(
                borrow_reserve_mint=borrow_mint,
                target_collateral_mint=target_mint,
                flash_amount=flash_micro,
                swap_amount=swap_micro,
                executor=jupiter,
                slippage_bps=slippage,
                borrow_reserve_pubkey=str(opp.get("borrow_reserve") or "").strip() or None,
                lending_market_pubkey=str(opp.get("market") or "").strip() or None,
            )

            async def _simulate(rpc_url: str) -> Any:
                async with AsyncClient(rpc_url) as sim_client:
                    return await sim_client.simulate_transaction(tx)

            sim = await call_with_rpc_fallback("sim", _simulate, label="collateral_sim")
            if sim.value.err is not None:
                tail_logs = (sim.value.logs or [])[-8:]
                logger.error(
                    "Collateral simulation failed: %s | logs=%s",
                    sim.value.err,
                    tail_logs,
                )
                self._log_attempt(opp, "sim_failed")
                return False

            wallet_safety.record_successful_simulation()
            ok_w, wreason = wallet_safety.before_live_send(flash_micro)
            if not ok_w:
                logger.warning("Collateral blocked by wallet safety: %s", wreason)
                self._log_attempt(opp, f"safety_{wreason}")
                return False

            tip_lamports = self._calculate_dynamic_tip(opp)
            import base64

            from src.dex.jupiter import send_signed_swap_transaction

            signed_b64 = base64.b64encode(bytes(tx)).decode("ascii")
            send_result = await send_signed_swap_transaction(
                signed_b64,
                tip_lamports=tip_lamports,
                keypair=kp,
            )
            landed = bool(send_result.get("success"))
            if not landed:
                logger.warning(
                    "Collateral Jito/RPC send failed | tip=%s err=%s path=%s",
                    tip_lamports,
                    send_result.get("error"),
                    send_result.get("send_path"),
                )
                self._log_attempt(opp, "bundle_fail")
                return False

            wallet_safety.record_live_trade_usdc_micro(flash_micro)
            profit_usd = float(opp.get("profit_usd") or 0)
            logger.warning(
                "Collateral LIVE FILL sent | net_bps=%.1f profit≈$%.2f tip=%s",
                net_bps,
                profit_usd,
                tip_lamports,
            )
            try:
                from src.core.risk import RiskEngine

                RiskEngine(self.settings).record_trade_result(profit_usd)
            except Exception as risk_exc:
                logger.debug("Collateral risk record failed: %s", risk_exc)
            try:
                from src.monitoring.metrics import record_fill

                record_fill("collateral_swap", summary={"profit_usdc": profit_usd})
            except Exception as metrics_exc:
                logger.debug("Collateral metrics record failed: %s", metrics_exc)
            try:
                from src.utils.inventory import replenish_if_low

                await replenish_if_low(force=True)
            except Exception as repl_exc:
                logger.debug("Post-fill USDC replenish failed: %s", repl_exc)
            try:
                from src.monitoring.capital_delta import log_capital_delta

                await log_capital_delta(
                    "collateral_live_fill",
                    strategy="collateral_swap",
                    extra={
                        "profit_usd": profit_usd,
                        "net_bps": net_bps,
                        "tip_lamports": tip_lamports,
                    },
                )
            except Exception as cap_exc:
                logger.debug("Collateral capital_delta failed: %s", cap_exc)
            self._log_attempt(opp, "success", extra={"tip_lamports": tip_lamports})
            return True

        except Exception as exc:
            logger.error(
                "Collateral execution exception: %s | market=%s borrow=%s target=%s",
                str(exc)[:300],
                market_label,
                borrow_mint[:8],
                target_mint[:8],
                exc_info=True,
            )
            self._write_exception_log(opp, exc)
            self._log_attempt(opp, "exception")
            return False
        finally:
            if client is not None:
                await client.close()

    def _log_attempt(
        self,
        opp: dict[str, Any],
        status: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "lane": "collateral_swap",
            "event": "COLLATERAL_ATTEMPT",
            "executed": status == "success",
            "live_fill": status == "success",
            "block_reason": status,
            "net_bps": float(opp.get("net_bps") or 0),
            "gross_bps": float(opp.get("gross_bps") or 0),
            "profit_usd": float(opp.get("profit_usd") or 0),
            "market": opp.get("market"),
            "borrow_mint": opp.get("borrow_mint"),
            "target_mint": opp.get("target_mint"),
        }
        if extra:
            record.update(extra)
        self._last_block_reason = status
        append_attempt(
            os.getenv("V2_ATTEMPTS_LOG", "logs/v2_attempts.jsonl"),
            record,
        )

    def log_scan(self, opportunities: list[dict[str, Any]], *, executed: bool = False) -> None:
        if not opportunities and not _env_bool("ENABLE_MEV_LOGGING", False):
            return
        top = opportunities[0] if opportunities else {}
        append_attempt(
            os.getenv("V2_ATTEMPTS_LOG", "logs/v2_attempts.jsonl"),
            {
                "lane": "collateral_swap",
                "event": "COLLATERAL_SCAN",
                "executed": executed,
                "live_fill": executed,
                "net_bps": float(top.get("net_bps") or 0),
                "gross_bps": float(top.get("gross_bps") or 0),
                "spread_bps": float(top.get("spread_bps") or 0),
                "profit_usd": float(top.get("profit_usd") or 0),
                "block_reason": "carry_found" if opportunities else "no_carry",
                "market": top.get("market"),
                "borrow_mint": top.get("borrow_mint"),
                "target_mint": top.get("target_mint"),
            },
        )


def get_collateral_executor(settings: Settings | None = None) -> CollateralExecutor:
    global _executor
    if _executor is None:
        _executor = CollateralExecutor(settings=settings)
    return _executor


def reset_collateral_executor() -> CollateralExecutor:
    global _executor
    _executor = CollateralExecutor()
    return _executor
