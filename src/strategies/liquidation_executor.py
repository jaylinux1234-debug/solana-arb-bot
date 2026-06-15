#!/usr/bin/env python3
"""Hardened Kamino liquidation scanner + KLend IDL execution."""

from __future__ import annotations

import logging
import os
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.config.settings import Settings, get_settings
from src.core.circuit_breaker import circuit_breaker
from src.strategies.brain_signals import note_liquidation_best
from src.v2.attempt_log import append_attempt

logger = logging.getLogger(__name__)

_executor: LiquidationExecutor | None = None
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


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


class LiquidationExecutor:
    """Scan near-liquidation Kamino obligations with realistic profit thresholds."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _min_util(self) -> float:
        return _env_float(
            "KAMINO_ALERT_MIN_UTIL",
            float(getattr(self.settings, "KAMINO_ALERT_MIN_UTIL", 0.92)),
        )

    def _min_ltv_ratio(self) -> float:
        return _env_float(
            "KAMINO_ALERT_MIN_LTV_RATIO",
            float(getattr(self.settings, "KAMINO_ALERT_MIN_LTV_RATIO", 0.92)),
        )

    def _min_profit_usdc(self) -> float:
        return _env_float(
            "LIQUIDATION_MIN_PROFIT_USDC",
            float(getattr(self.settings, "LIQUIDATION_MIN_PROFIT_USDC", 3.5)),
        )

    def _max_flash_micro(self) -> int:
        max_usdc = _env_float(
            "LIQUIDATION_MAX_FLASH_USDC",
            float(getattr(self.settings, "LIQUIDATION_MAX_FLASH_USDC", 35)),
        )
        tier_cap = _env_int("LIQUIDATION_MAX_TRADE_USDC_MICRO", 35_000_000)
        return min(int(max_usdc * 1_000_000), tier_cap)

    def _liquidation_bonus(self) -> float:
        return _env_float("LIQUIDATION_BONUS_PCT", 0.05)

    def _net_retain_frac(self) -> float:
        return _env_float("LIQUIDATION_NET_RETAIN_FRAC", 0.65)

    def _partial_pct(self) -> float:
        pct = _env_float("LIQUIDATION_PARTIAL_PCT", 0.5)
        return max(0.01, min(1.0, pct))

    @staticmethod
    def partial_liquidity_amount(
        debt_amt: int,
        flash_amount: int,
        *,
        partial_pct: float = 0.5,
    ) -> int:
        """Cap flash borrow by configured partial liquidation fraction of debt."""
        pct = max(0.01, min(1.0, float(partial_pct)))
        if debt_amt <= 0:
            return max(1, int(flash_amount))
        partial = max(1, int(debt_amt * pct))
        return min(int(flash_amount), partial)

    def _estimate_liquidation_profit(self, obl: dict[str, Any]) -> float:
        """Realistic profit after slippage, flash fee, and Jito drag."""
        debt_usdc = float(obl.get("debt_usdc") or 0.0)
        if debt_usdc <= 0:
            try:
                debt_usdc = int(obl.get("debt_amount") or 0) / 1_000_000.0
            except (TypeError, ValueError):
                debt_usdc = 0.0
        if debt_usdc <= 0:
            return 0.0

        bonus = float(obl.get("liquidation_bonus") or self._liquidation_bonus())
        gross = debt_usdc * bonus
        return gross * self._net_retain_frac()

    async def _fetch_liquidatable_obligations(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Delegate to hardened LiquidationBot fetch with env thresholds."""
        from solana.rpc.async_api import AsyncClient

        from src.core.rpc_config import resolve_rpc_url
        from src.dex.jupiter import get_jupiter_executor
        from src.strategies.liquidation import LiquidationBot

        if not _env_bool("ENABLE_LIQUIDATION_MONITORING", True):
            return []

        jup = get_jupiter_executor(self.settings)
        if jup.keypair is None:
            return []

        rpc = resolve_rpc_url("default")
        async with AsyncClient(rpc) as client:
            bot = LiquidationBot(client, jup, jup.keypair)
            return await bot.fetch_liquidatable_positions(limit=limit)

    async def scan_opportunities(self) -> list[dict[str, Any]]:
        """Hardened scanner with realistic thresholds."""
        if circuit_breaker.should_pause():
            return []

        obligations = await self._fetch_liquidatable_obligations()
        min_profit = self._min_profit_usdc()
        max_flash = self._max_flash_micro()
        opportunities: list[dict[str, Any]] = []

        for obl in obligations:
            profit = self._estimate_liquidation_profit(obl)
            if profit <= min_profit:
                continue
            try:
                debt_micro = int(obl.get("debt_amount") or 0)
            except (TypeError, ValueError):
                debt_micro = 0
            opportunities.append(
                {
                    "type": "liquidation",
                    "obligation": obl.get("obligation"),
                    "owner": obl.get("owner"),
                    "lending_market": obl.get("lending_market"),
                    "profit_usd": round(profit, 4),
                    "profit_usdc": round(profit, 4),
                    "health_factor": obl.get("health_factor"),
                    "debt_mint": obl.get("debt_mint"),
                    "debt_amount": debt_micro,
                    "debt_usdc": debt_micro / 1_000_000.0 if debt_micro else 0.0,
                    "size_usdc_micro": min(max_flash, debt_micro) if debt_micro else max_flash,
                    "collateral_reserve": obl.get("collateral_reserve"),
                    "debt_reserve": obl.get("debt_reserve"),
                    "active": True,
                }
            )

        opportunities.sort(key=lambda o: float(o.get("profit_usd") or 0), reverse=True)
        if opportunities:
            top = opportunities[0]
            logger.info(
                "Liquidation scan | obligation=%s profit=$%.2f hf=%s",
                str(top.get("obligation", ""))[:12],
                float(top.get("profit_usd") or 0),
                top.get("health_factor"),
            )
        return opportunities

    async def refresh_brain_snapshot(self) -> None:
        """Update brain liquidation_best from latest scan."""
        opps = await self.scan_opportunities()
        if not opps:
            note_liquidation_best({"active": False, "profit_usdc": 0.0})
            return
        top = opps[0]
        note_liquidation_best(
            {
                **top,
                "active": float(top.get("profit_usdc") or 0) >= self._min_profit_usdc(),
            }
        )

    def _liquidation_slippage_bps(self) -> int:
        return _env_int("LIQUIDATION_SLIPPAGE_BPS", 200)

    def _ai_min_confidence(self) -> int:
        return max(0, min(100, _env_int("LIQUIDATION_AI_MIN_CONFIDENCE", 55)))

    def _dynamic_tip(self, opp: dict[str, Any]) -> int:
        from src.dex.jupiter import resolve_execution_jito_tip_lamports

        profit = float(opp.get("profit_usd") or opp.get("profit_usdc") or 0)
        size_micro = int(opp.get("size_usdc_micro") or self._max_flash_micro())
        env_tip = (os.getenv("LIQUIDATION_JITO_TIP_LAMPORTS") or "").strip()
        if env_tip:
            return int(env_tip)
        return int(
            resolve_execution_jito_tip_lamports(
                0.0,
                size_usdc_micro=size_micro,
                gross_bps=0.0,
                override_net_usd=profit if profit > 0 else None,
            )
        )

    def _write_exception_log(self, opp: dict[str, Any], exc: Exception) -> None:
        path = Path(os.getenv("MEV_EXCEPTIONS_LOG", "logs/mev_exceptions.log"))
        path.parent.mkdir(parents=True, exist_ok=True)
        obligation = str(opp.get("obligation") or "")[:12]
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"{datetime.now(UTC).isoformat()} | lane=liquidation obligation={obligation} | "
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}\n"
            )

    async def execute(self, opp: dict[str, Any]) -> bool:
        """Flash borrow → KLend liquidate → flash repay via Jito bundle."""
        import src.core.wallet as wallet_safety
        from solana.rpc.async_api import AsyncClient

        from src.cex.price_feed import cex_feed
        from src.core.rpc_config import call_with_rpc_fallback
        from src.core.rpc_urls import resolve_rpc_url
        from src.dex.jupiter import JupiterExecutor
        from src.execution.jito_bundle import get_jito_bundle_executor
        from src.strategies.liquidation import LiquidationBot
        from src.utils.ai import ai_agent_decide

        profit = float(opp.get("profit_usd") or opp.get("profit_usdc") or 0)
        obligation = str(opp.get("obligation") or "")
        debt_reserve = str(opp.get("debt_reserve") or "").strip()
        collateral_reserve = str(opp.get("collateral_reserve") or "").strip()

        if profit < self._min_profit_usdc():
            self._log_attempt(opp, "below_threshold")
            return False

        if not debt_reserve or not collateral_reserve:
            logger.warning(
                "Liquidation missing reserves | debt=%s collateral=%s",
                debt_reserve[:8] if debt_reserve else "?",
                collateral_reserve[:8] if collateral_reserve else "?",
            )
            self._log_attempt(opp, "missing_reserves")
            return False

        if circuit_breaker.should_pause():
            self._log_attempt(opp, "circuit_breaker")
            return False

        client: AsyncClient | None = None
        try:
            jupiter = JupiterExecutor(settings=self.settings)
            kp = jupiter.keypair
            if kp is None:
                logger.warning("Liquidation execute: no signing keypair")
                self._log_attempt(opp, "no_signer")
                return False

            rpc = resolve_rpc_url("default")
            client = AsyncClient(rpc)
            from src.core.rpc_config import get_robust_sol_balance

            sol_lamports = int(
                max(0.0, await get_robust_sol_balance(str(kp.pubkey()))) * 1_000_000_000
            )

            payload = {
                "strategy": "kamino_liquidation",
                **opp,
                "profit_usdc": profit,
            }
            payload["cex_prices"] = await cex_feed.get_multiple_prices(["SOL/USDC"])
            ai_min = self._ai_min_confidence()
            decision = await ai_agent_decide(payload, sol_lamports, min_confidence=ai_min)
            if decision.get("final_action") != "APPROVE":
                td = decision.get("trade_decision") or {}
                logger.info("[AI REJECT] liquidation: %s", td.get("reasoning", ""))
                self._log_attempt(opp, "ai_reject")
                return False

            try:
                conf = int((decision.get("trade_decision") or {}).get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0
            if conf <= self._ai_min_confidence():
                self._log_attempt(opp, "ai_low_confidence", extra={"confidence": conf})
                return False

            if _env_bool("TEST_MODE", self.settings.test_mode):
                logger.info(
                    "TEST_MODE liquidation success for %s",
                    (obligation[:8] if obligation else "?"),
                )
                self._log_attempt(opp, "test_mode")
                return True

            flash_micro = int(opp.get("size_usdc_micro") or self._max_flash_micro())
            bot = LiquidationBot(client, jupiter, kp)
            tx = await bot.build_liquidation_tx(opp, flash_amount=flash_micro)

            async def _simulate(rpc_url: str) -> Any:
                async with AsyncClient(rpc_url) as sim_client:
                    return await sim_client.simulate_transaction(tx)

            sim = await call_with_rpc_fallback("sim", _simulate, label="liquidation_sim")
            if sim.value.err is not None:
                logger.error("Liquidation simulation failed: %s", sim.value.err)
                self._log_attempt(opp, "sim_failed")
                return False

            wallet_safety.record_successful_simulation()
            ok_w, wreason = wallet_safety.before_live_send(flash_micro)
            if not ok_w:
                logger.warning("Liquidation blocked by wallet safety: %s", wreason)
                self._log_attempt(opp, f"safety_{wreason}")
                return False

            tip_lamports = self._dynamic_tip(opp)
            jito = get_jito_bundle_executor()
            landed = await jito.send_bundle([tx], priority_fee=tip_lamports)
            if not landed:
                logger.warning("Liquidation Jito bundle rejected | tip=%s", tip_lamports)
                self._log_attempt(opp, "jito_reject", extra={"tip_lamports": tip_lamports})
                return False

            wallet_safety.record_live_trade_usdc_micro(flash_micro)
            logger.info(
                "FIRST MEV FILL — Liquidation! profit=$%.2f | obligation=%s tip=%s",
                profit,
                obligation[:12] or "?",
                tip_lamports,
            )
            self._log_attempt(opp, "success", extra={"tip_lamports": tip_lamports})
            return True

        except Exception as exc:
            logger.error("Liquidation execute exception: %s", str(exc)[:400], exc_info=True)
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
            "lane": "liquidation",
            "event": "LIQUIDATION_ATTEMPT",
            "executed": status in ("success", "test_mode"),
            "live_fill": status == "success",
            "block_reason": status,
            "profit_usd": float(opp.get("profit_usd") or opp.get("profit_usdc") or 0),
            "health_factor": opp.get("health_factor"),
            "obligation": opp.get("obligation"),
            "size_usdc_micro": opp.get("size_usdc_micro"),
        }
        if extra:
            record.update(extra)
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
                "lane": "liquidation",
                "event": "LIQUIDATION_SCAN",
                "executed": executed,
                "live_fill": executed,
                "profit_usd": float(top.get("profit_usd") or 0),
                "health_factor": top.get("health_factor"),
                "obligation": top.get("obligation"),
                "block_reason": "liq_found" if opportunities else "no_liq",
            },
        )


def get_liquidation_executor(settings: Settings | None = None) -> LiquidationExecutor:
    global _executor
    if _executor is None:
        _executor = LiquidationExecutor(settings=settings)
    return _executor


def reset_liquidation_executor() -> LiquidationExecutor:
    global _executor
    _executor = LiquidationExecutor()
    return _executor
