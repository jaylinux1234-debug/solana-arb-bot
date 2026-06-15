#!/usr/bin/env python3
"""
CEX-DEX Arbitrage Engine — Backpack ↔ Jupiter (Solana).

Optimized for real edges (8–25+ bps) with dynamic costs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from typing import Any

from src.config.settings import Settings, get_settings
from src.core.circuit_breaker import circuit_breaker
from src.core.onchain_profit import assert_roundtrip_profit, fetch_usdc_balance_micro
from src.core.wallet import check_global_safety
from src.core.wallet_safety import wallet_safety as safety_store
from src.cex.backpack import BackpackClient
from src.dex.jupiter import SOL_MINT, JupiterClient
from src.core.risk import RiskEngine
from src.execution.jito import JitoMultiRelay, configure_jito
from src.monitoring.metrics import (
    record_cex_dex_near_miss,
    record_trade_execution,
    record_trade_signal,
)
from src.monitoring.win_rate_tracker import LIVE_MIN_WIN_RATE, get_win_rate_tracker
from src.strategies.cex_dex import build_cex_dex_flash_tx, simulate
from src.strategies.cex_dex_core import (
    analyze_cex_dex_spread,
    clamp_trade_usdc_micro,
    dynamic_min_trade_usdc_micro,
    gate_cex_dex_ask_depth,
    gate_cex_dex_direction,
    net_spread_bps_after_costs,
    set_cex_cheap_flags,
)
from src.strategies.cex_dex_cycle import (
    get_cex_executor,
)
from src.strategies.cex_dex_cycle import (
    get_jupiter_executor_for_cycle as get_jupiter_executor,
)
from src.core.capital_preflight import InsufficientBalance, preflight_check
from src.strategies.cex_dex_roundtrip import roundtrip_sim_min_net_bps
from src.strategies.evaluate_roundtrip import (
    log_roundtrip_near_miss,
    roundtrip_min_net_bps,
    should_execute_roundtrip,
)
from src.strategies.roundtrip_sim import RoundtripSimulator
from src.utils.ai import get_ai_approval
from src.utils.dynamic_cost_model import cost_model
from src.utils.price import bps_diff

logger = logging.getLogger(__name__)


class CexDexArbitrage:
    """CEX-DEX arb: oracle → gross/net gates → AI → sim → execute."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.last_trade_time = 0.0
        self.volatility_ema = 12.0
        self._probe_micro = int(
            os.getenv(
                "CEX_DEX_PROBE_USDC_MICRO",
                str(self.settings.cex_dex_probe_usdc_micro),
            )
        )
        self._cex = get_cex_executor()
        self._jup = get_jupiter_executor()
        if self._jup.keypair is not None:
            configure_jito(self._jup.client, self._jup.keypair)
        self._jito = JitoMultiRelay(client=self._jup.client, keypair=self._jup.keypair)
        self._ai_floor = max(
            self.settings.ai_approve_min_confidence,
            self.settings.cex_dex_ai_confidence_floor,
        )
        self.win_rate_tracker = get_win_rate_tracker()

    async def get_prices(
        self,
    ) -> tuple[float, float, float, str, float | None, float | None]:
        """CEX buy (ask), Jupiter USDC/SOL, gross bps, direction, mid, ask."""
        cex_price, cex_mid, cex_ask = await self._cex.get_cex_buy_reference_price(
            "SOL_USDC"
        )
        if not cex_price or cex_price <= 0:
            raise ValueError("CEX buy reference price unavailable")

        jup_price, _quote = await self._jup.get_implied_usdc_per_sol(self._probe_micro)
        from src.dex.executor import get_dex_executor

        dex_q = await get_dex_executor().get_best_dex_price(
            self._probe_micro,
            use_phoenix=True,
            jupiter_price=jup_price,
        )
        if dex_q and dex_q.price > 0:
            jup_price = dex_q.price
        if not jup_price or jup_price <= 0:
            raise ValueError("DEX price unavailable")

        spread = analyze_cex_dex_spread(cex_price, jup_price)
        if spread is None:
            raise ValueError("spread analysis failed")

        gross_bps = abs(float(bps_diff(cex_price, jup_price)))
        self.volatility_ema = 0.7 * self.volatility_ema + 0.3 * gross_bps
        return (
            float(cex_price),
            float(jup_price),
            gross_bps,
            spread.direction,
            float(cex_mid) if cex_mid and cex_mid > 0 else None,
            float(cex_ask) if cex_ask and cex_ask > 0 else None,
        )

    def _net_bps(self, gross_bps: float, size_usdc_micro: int, direction: str) -> float:
        """Model net spread after size- and direction-aware costs."""
        return net_spread_bps_after_costs(
            gross_bps,
            size_usdc_micro,
            direction=direction,  # type: ignore[arg-type]
        )

    def _propose_size(self, gross_bps: float, cex_mid: float) -> int:
        util = self.settings.cex_dex_depth_utilization
        edge_scale = min(1.0, gross_bps / 200.0)
        raw = int(self.settings.cex_dex_max_trade_usdc_micro * util * edge_scale)
        flash_cap = int(os.getenv("CEX_DEX_FLASH_AMOUNT_USDC_MICRO", "50000000"))
        liq_cap = int(50.0 * cex_mid * util * 1_000_000)
        min_trade = dynamic_min_trade_usdc_micro(gross_bps, settings=self.settings)
        capped = clamp_trade_usdc_micro(
            max_trade_usdc_micro=self.settings.cex_dex_max_trade_usdc_micro,
            flash_cap_usdc_micro=flash_cap,
            liquidity_cap_usdc_micro=liq_cap,
            min_trade_usdc_micro=min_trade,
        )
        return max(min_trade, min(raw, capped) if raw > 0 else capped)

    async def evaluate_opportunity(self) -> dict[str, Any] | None:
        if not check_global_safety() or circuit_breaker.should_pause():
            return None

        try:
            cex_price, jup_price, gross_bps, direction, cex_mid, cex_ask = (
                await self.get_prices()
            )
        except ValueError as exc:
            logger.debug("CEX-DEX oracle skip: %s", exc)
            return None

        scan_ctx: dict[str, Any] = {
            "symbol": "SOL",
            "backpack_symbol": "SOL_USDC",
            "direction": direction,
            "cex_price": cex_price,
            "jup_price": jup_price,
            "cex_mid": cex_mid,
            "cex_ask": cex_ask,
            "size_usdc_micro": self._probe_micro,
        }
        set_cex_cheap_flags(scan_ctx, direction)
        dir_reject = gate_cex_dex_direction(scan_ctx)
        if dir_reject:
            if self.settings.cex_dex_log_near_misses:
                logger.info(
                    "CEX-DEX NEAR_MISS | %s gross=%.2f cex_buy=%.4f jup=%.4f",
                    dir_reject["status"],
                    gross_bps,
                    cex_price,
                    jup_price,
                )
                record_cex_dex_near_miss(gross_bps, reason=dir_reject["status"])
            return None

        dynamic_min_gross = max(
            6,
            int(
                self.settings.cex_dex_min_gross_spread_bps
                * max(0.5, self.volatility_ema / 18.0)
            ),
        )

        if gross_bps < dynamic_min_gross:
            if self.settings.cex_dex_log_near_misses:
                logger.info(
                    "CEX-DEX NEAR_MISS | gross=%.2f (need ~%d) cex_buy=%.4f jup=%.4f vol=%.1f",
                    gross_bps,
                    dynamic_min_gross,
                    cex_price,
                    jup_price,
                    self.volatility_ema,
                )
                record_cex_dex_near_miss(gross_bps)
            return None

        size_usdc_micro = self._propose_size(
            gross_bps, cex_mid if cex_mid and cex_mid > 0 else cex_price
        )
        scan_ctx["size_usdc_micro"] = size_usdc_micro
        depth_reject = await gate_cex_dex_ask_depth(self._cex, scan_ctx)
        if depth_reject:
            if self.settings.cex_dex_log_near_misses:
                logger.info(
                    "CEX-DEX NEAR_MISS | %s size=$%.2f gross=%.2f",
                    depth_reject["status"],
                    size_usdc_micro / 1_000_000,
                    gross_bps,
                )
                record_cex_dex_near_miss(gross_bps, reason=depth_reject["status"])
            return None

        net_bps = self._net_bps(gross_bps, size_usdc_micro, direction)
        min_net = float(self.settings.cex_dex_min_net_spread_bps) + float(
            self.settings.cex_dex_edge_safety_bps
        )

        if net_bps < min_net:
            if self.settings.cex_dex_log_near_misses:
                logger.info(
                    "CEX-DEX NEAR_MISS | net_gate gross=%.2f net=%.2f need=%.1f dir=%s",
                    gross_bps,
                    net_bps,
                    min_net,
                    direction,
                )
                record_cex_dex_near_miss(gross_bps)
            return None

        signal: dict[str, Any] = {
            "strategy": "cex_dex",
            "symbol": "SOL",
            "cex_price": cex_price,
            "cex_mid": cex_mid if cex_mid and cex_mid > 0 else cex_price,
            "cex_ask": cex_ask,
            "jup_price": jup_price,
            "gross_bps": gross_bps,
            "net_bps": net_bps,
            "size_usdc_micro": size_usdc_micro,
            "size_usdc": size_usdc_micro,
            "direction": direction,
            "is_cex_cheap": True,
            "timestamp": time.time(),
        }

        logger.info(
            "CEX-DEX SIGNAL | gross=%.2f net=%.2f size=$%.2f vol=%.1f",
            gross_bps,
            net_bps,
            size_usdc_micro / 1_000_000,
            self.volatility_ema,
        )
        record_trade_signal("cex_dex", gross_bps, net_bps)
        return signal

    async def execute(self, signal: dict[str, Any]) -> bool:
        """AI → sim → Jito bundle."""
        cooldown = int(
            os.getenv(
                "LIVE_TRADE_COOLDOWN_SECONDS",
                str(self.settings.live_trade_cooldown_seconds),
            )
        )
        if time.time() - self.last_trade_time < cooldown:
            logger.info("Cooldown active, skipping")
            return False

        can, reason = await safety_store.can_trade(int(signal["size_usdc_micro"]))
        if not can:
            logger.info("Wallet safety blocked: %s", reason)
            return False

        approval = await get_ai_approval(
            signal_type="cex_dex",
            gross_bps=float(signal["gross_bps"]),
            cex_mid=float(signal.get("cex_price") or signal["cex_mid"]),
            jup_price=float(signal["jup_price"]),
            size_usdc_micro=int(signal["size_usdc_micro"]),
            net_bps=float(signal["net_bps"]),
            direction=signal.get("direction"),
        )
        if not approval.approve or approval.confidence < self._ai_floor:
            logger.info(
                "AI REJECTED | confidence=%s need=%s reason=%s",
                approval.confidence,
                self._ai_floor,
                approval.reason,
            )
            return False

        conf = max(1, approval.confidence)
        min_trade = dynamic_min_trade_usdc_micro(
            float(signal.get("gross_bps") or 0),
            settings=self.settings,
        )
        size = max(min_trade, int(int(signal["size_usdc_micro"]) * conf / 100))

        if not await self._roundtrip_sim_gate(signal, size):
            return False

        if not await self._simulate_trade(signal, size):
            return False

        if (
            not self.settings.test_mode
            and not self.settings.simulate
            and not self.win_rate_tracker.should_approve(min_win_rate=LIVE_MIN_WIN_RATE)
        ):
            logger.info("Win rate below threshold - skipping")
            record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
            return False

        trade_id = str(signal.get("trade_id") or f"arb-{uuid.uuid4().hex[:12]}")
        signal["trade_id"] = trade_id

        if await self._execute_live(signal, size, trade_id=trade_id):
            self.last_trade_time = time.time()
            cost_model.record_execution(
                {
                    "gross_bps": signal["gross_bps"],
                    "net_bps": signal["net_bps"],
                    "realized_net_bps": signal["net_bps"],
                    "size_usdc_micro": size,
                }
            )
            logger.info(
                "CEX-DEX TRADE EXECUTED | size=$%.2f net=%.2f bps",
                size / 1_000_000,
                signal["net_bps"],
            )
            return True
        return False

    async def get_wallet_sol(self) -> float:
        try:
            from src.core.wallet import get_sol_balance

            return float(await get_sol_balance() or 0.0)
        except Exception:
            return 0.0

    async def get_cex_sol_balance(self) -> float:
        try:
            bal = await self._cex.get_balance("SOL")
            return float(bal or 0.0)
        except Exception:
            return 0.0

    async def should_execute(self, signal: dict[str, Any]) -> bool:
        """PATH-aware AdvancedCostModel gate with GO_LIVE soft-pass."""
        gross_bps = float(signal.get("gross_bps") or 0)
        if gross_bps < float(os.getenv("CEX_DEX_MIN_GROSS_SPREAD_BPS", 6)):
            return False

        wallet_sol = await self.get_wallet_sol()
        cex_sol = await self.get_cex_sol_balance()
        ok, cost = should_execute_roundtrip(
            signal,
            wallet_sol=wallet_sol,
            cex_sol=cex_sol,
        )
        signal["roundtrip_model_net_bps"] = cost.net_bps
        signal["roundtrip_model_cost_bps"] = cost.total_cost_bps
        signal["roundtrip_breakdown"] = cost.breakdown
        if ok:
            return True
        log_roundtrip_near_miss(signal, cost, lane="cex_dex")
        return False

    async def evaluate_roundtrip(
        self,
        quote_data: dict[str, Any],
        size_usdc: int,
    ) -> bool:
        """AdvancedCostModel gate with GO_LIVE soft-pass."""
        signal = {
            **quote_data,
            "size_usdc": size_usdc,
            "size_usdc_micro": size_usdc,
        }
        ok = await self.should_execute(signal)
        quote_data["roundtrip_model_net_bps"] = signal.get("roundtrip_model_net_bps")
        quote_data["roundtrip_model_cost_bps"] = signal.get("roundtrip_model_cost_bps")
        quote_data["roundtrip_breakdown"] = signal.get("roundtrip_breakdown")
        if ok:
            min_net = roundtrip_min_net_bps()
            quote_data["roundtrip_reason"] = (
                "roundtrip_soft_pass"
                if float(signal.get("roundtrip_model_net_bps") or 0) < min_net
                else "roundtrip_strong"
            )
        return ok

    async def _roundtrip_sim_gate(self, signal: dict[str, Any], size: int) -> bool:
        """Advanced cost model + optional Jupiter live quote confirmation."""
        if signal.get("direction", "dex_cheap") != "cex_cheap":
            return True

        cex_buy, _cex_mid, cex_ask = await self._cex.get_cex_buy_reference_price(
            "SOL_USDC"
        )
        if cex_buy and cex_buy > 0:
            signal["cex_price"] = float(cex_buy)
            signal["cex_ask"] = cex_ask

        cex_px = float(signal.get("cex_price") or signal.get("cex_mid") or 0)
        if cex_px <= 0:
            logger.info("Roundtrip sim skipped — no CEX buy price")
            return False

        quote_data = {
            "gross_bps": float(signal.get("gross_bps") or 0),
            "vol_5m_pct": float(signal.get("vol_pct") or self.volatility_ema / 100.0),
            "cex_spread_bps": float(signal.get("cex_spread_bps") or 0),
        }
        if not await self.evaluate_roundtrip(quote_data, size):
            record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
            return False

        signal["roundtrip_model_net_bps"] = quote_data.get("roundtrip_model_net_bps")
        signal["roundtrip_reason"] = quote_data.get("roundtrip_reason")

        live_confirm = os.getenv("CEX_DEX_ROUNDTRIP_LIVE_QUOTE_CONFIRM", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not live_confirm:
            signal["roundtrip_sim_net_bps"] = float(
                quote_data.get("roundtrip_model_net_bps") or 0
            )
            return True

        min_net = roundtrip_sim_min_net_bps()
        roundtrip = RoundtripSimulator(self._jup, settings=self.settings)
        sim_ok, simulated_net_bps, reason, _details = await roundtrip.run_roundtrip(
            cex_px,
            size,
            base_mint=str(signal.get("base_mint") or SOL_MINT),
            base_decimals=int(signal.get("base_decimals") or 9),
            expected_net_bps=float(signal.get("net_bps") or 0),
        )
        if not sim_ok:
            logger.info(
                "Roundtrip live quote failed | net=%.2f min=%.1f reason=%s",
                simulated_net_bps,
                min_net,
                reason,
            )
            record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
            return False
        signal["roundtrip_sim_net_bps"] = simulated_net_bps
        return True

    def _record_win_rate_outcome(
        self,
        signal: dict[str, Any],
        *,
        success: bool,
        realized_usdc: float,
        trade_id: str,
        tx_sig: str = "",
    ) -> None:
        gross_bps = float(signal.get("gross_bps") or 0)
        net_bps = float(signal.get("net_bps") or 0)
        pair = str(signal.get("pair_label") or "SOL/USDC")
        size_micro = int(signal.get("size_usdc_micro") or 0)
        size_usdc = size_micro / 1_000_000.0
        tx_signature = str(
            tx_sig or signal.get("tx_sig") or signal.get("bundle_id") or ""
        ).strip()

        self.win_rate_tracker.record_trade(
            trade_id,
            gross_bps,
            net_bps,
            realized_usdc,
            success,
            pair=pair,
        )
        if not self.settings.test_mode and not self.settings.simulate:
            try:
                from src.execution.trade_logger import log_execution_trade

                hops = int(signal.get("route_hops") or signal.get("jupiter_route_hops") or 0)
                log_execution_trade(
                    pair=pair,
                    gross_bps=gross_bps,
                    net_bps=net_bps,
                    size_usdc=size_usdc,
                    success=success,
                    realized_usdc=realized_usdc,
                    tx_sig=tx_signature,
                    trade_id=trade_id,
                    hops=hops,
                )
            except Exception as exc:
                logger.debug("log_execution_trade skipped: %s", exc)

    async def _simulate_trade(self, signal: dict[str, Any], size: int) -> bool:
        if not await self._jup.has_signing():
            logger.warning("CEX-DEX sim skipped: no signing backend")
            return self.settings.test_mode

        cex_px = float(signal.get("cex_price") or signal.get("cex_mid") or 0)
        tx = await build_cex_dex_flash_tx(
            cex_px,
            signal["jup_price"],
            size,
            client=self._jup.client,
            keypair=self._jup.keypair,
            jupiter=self._jup,
            direction=signal.get("direction", "dex_cheap"),
        )
        if tx is None:
            return False
        ok = await simulate(self._jup.client, tx)
        if ok:
            safety_store.record_successful_simulation()
        return ok

    async def _execute_live(
        self,
        signal: dict[str, Any],
        size: int,
        *,
        trade_id: str,
    ) -> bool:
        if self.settings.test_mode:
            logger.info(
                "[TEST] CEX-DEX would execute $%.2f",
                size / 1_000_000,
            )
            return True

        if not self.settings.live_trading_confirm_enabled:
            logger.warning("LIVE_TRADING_CONFIRM not set — skipping live send")
            return False

        if not await self._jup.has_signing():
            logger.error("Cannot execute: no signing keypair (SIGNER_TYPE=hot)")
            return False

        try:
            await preflight_check(size)
        except InsufficientBalance as exc:
            logger.warning("BLOCKED: capital preflight | %s", exc)
            record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
            return False

        usdc_before = await fetch_usdc_balance_micro()
        direction = signal.get("direction", "dex_cheap")
        cex_px = float(signal.get("cex_price") or signal.get("cex_mid") or 0)
        if direction == "cex_cheap":
            order = await self._cex.buy_sol(size, price=cex_px)
            if not order:
                return False
            await asyncio.sleep(self.settings.cex_withdrawal_buffer_sec)

        tx = await build_cex_dex_flash_tx(
            cex_px,
            signal["jup_price"],
            size,
            client=self._jup.client,
            keypair=self._jup.keypair,
            jupiter=self._jup,
            direction=direction,
        )
        if tx is None:
            return False

        tip = self._calc_tip(signal, size)
        if self._jup.keypair is not None:
            bundle_id = await self._jito.send_bundle([tx], tip, append_tip_tx=True)
        else:
            tip_tx = await self._jup.build_signed_tip_transaction(tip)
            if tip_tx is None:
                return False
            bundle_id = await self._jito.send_bundle(
                [tx, tip_tx], tip_lamports=0, append_tip_tx=False
            )
        if not bundle_id:
            self._record_win_rate_outcome(
                signal,
                success=False,
                realized_usdc=0.0,
                trade_id=trade_id,
                tx_sig="",
            )
            record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
            return False

        signal["tx_sig"] = str(bundle_id)
        safety_store.record_live_trade_usdc_micro(size)

        if usdc_before is not None:
            await asyncio.sleep(float(os.getenv("ONCHAIN_PROFIT_SETTLE_SEC", "2.5")))
            usdc_after = await fetch_usdc_balance_micro()
            if usdc_after is not None:
                ok_profit, details = await assert_roundtrip_profit(
                    usdc_before_micro=usdc_before,
                    usdc_after_micro=usdc_after,
                    trade_size_micro=size,
                    expected_net_bps=float(signal.get("net_bps") or 0),
                    settings=self.settings,
                )
                realized_bps = float(details.get("realized_bps") or 0)
                min_assert = int(
                    os.getenv(
                        "ONCHAIN_PROFIT_ASSERT_BPS",
                        str(self.settings.ONCHAIN_PROFIT_ASSERT_BPS),
                    )
                )
                if not ok_profit or realized_bps < float(min_assert):
                    logger.warning(
                        "Profit assert failed | realized=%.1fbps need>=%dbps bundle=%s",
                        realized_bps,
                        min_assert,
                        bundle_id,
                    )
                    self._record_win_rate_outcome(
                        signal,
                        success=False,
                        realized_usdc=float(details.get("delta_micro") or 0) / 1_000_000.0,
                        trade_id=trade_id,
                        tx_sig=str(bundle_id),
                    )
                    record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
                    circuit_breaker.trip("onchain_profit_assert_failed")
                    return False

                realized_usdc = int(details.get("delta_micro") or 0) / 1_000_000.0
                self._record_win_rate_outcome(
                    signal,
                    success=True,
                    realized_usdc=realized_usdc,
                    trade_id=trade_id,
                    tx_sig=str(bundle_id),
                )
                record_trade_execution("cex_dex", success=True, pnl_usd=realized_usdc)
                signal["realized_net_bps"] = realized_bps
                return True

        self._record_win_rate_outcome(
            signal,
            success=True,
            realized_usdc=0.0,
            trade_id=trade_id,
            tx_sig=str(bundle_id),
        )
        record_trade_execution("cex_dex", success=True, pnl_usd=0.0)
        return True

    def _calc_tip(self, signal: dict[str, Any], size: int) -> int:
        from src.execution.jito_bundle import resolve_jito_tip_lamports

        return resolve_jito_tip_lamports(
            self.settings,
            net_bps=float(signal.get("net_bps") or signal.get("gross_bps") or 0),
            size_usdc_micro=int(size),
        )


cex_dex_arb = CexDexArbitrage()


async def run_cex_dex_cycle() -> bool:
    """One detect → execute cycle (used by ``src.main``)."""
    settings = get_settings()
    signal = await cex_dex_arb.evaluate_opportunity()
    if not signal:
        return False

    if signal["net_bps"] > settings.cex_dex_min_net_spread_bps:
        logger.info(
            "CEX-DEX EXECUTE_CANDIDATE | gross=%.2f net=%.2f — AI/sim/execute",
            signal["gross_bps"],
            signal["net_bps"],
        )
        return await cex_dex_arb.execute(signal)

    return False


class CexDexArbitrageBot:
    """Poll ``run_cex_dex_cycle`` or legacy ``CexDexCycle`` (``CEX_DEX_USE_CYCLE=1``)."""

    def __init__(self, *, poll_interval_sec: float | None = None) -> None:
        self.poll_interval_sec = poll_interval_sec or float(
            os.getenv("CEX_DEX_POLL_INTERVAL_SEC", "2.5")
        )
        self._helius_mode = os.getenv("ENABLE_HELIUS_WEBHOOK", "false").lower() == "true"
        if self._helius_mode:
            logger.info("Helius webhook mode ACTIVE — polling minimized")
        self._use_legacy_cycle = os.getenv("CEX_DEX_USE_CYCLE", "").lower() in (
            "1",
            "true",
            "yes",
        )

    async def run(self) -> None:
        if self._use_legacy_cycle:
            from src.strategies.cex_dex_cycle import cex_dex_cycle

            await cex_dex_cycle.run_forever()
            return

        while True:
            try:
                await run_cex_dex_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("cex_dex_arb cycle error: %s", exc, exc_info=True)
            delay = self.poll_interval_sec * (0.85 + random.random() * 0.3)
            await asyncio.sleep(delay)


async def create_cex_dex_bot(**kwargs: Any) -> CexDexArbitrageBot:
    return CexDexArbitrageBot(**kwargs)


# Re-export reverse lane (canonical module: dex_cex_reverse.py)
from src.strategies.dex_cex_reverse import DexCexReverseStrategy  # noqa: F401
