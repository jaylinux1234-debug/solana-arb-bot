# src/strategies/cex_dex_cycle.py
"""Single CEX-DEX cycle: oracle → spread gate → AI → sim → Kamino/Jito execute."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass

import src.core.wallet as wallet_safety
from src.cex.executor import CexExecutor, cex_executor
from src.config.settings import settings
from src.core.secure_secrets import signer_type, skip_hot_secret_files
from src.core.circuit_breaker import circuit_breaker
from src.core.wallet import check_global_safety
from src.core.wallet_safety import wallet_safety as safety_store
from src.dex.jupiter import JupiterExecutor, get_jupiter_executor
from src.execution.jito import JitoMultiRelay, configure_jito
from src.monitoring.metrics import record_cex_dex_near_miss
from src.strategies.brain_signals import note_cex_dex_context
from src.strategies.cex_dex import (
    build_cex_dex_flash_tx,
    simulate,
)
from src.strategies.cex_dex_core import (
    analyze_cex_dex_spread,
    cex_dex_ai_min_confidence,
    clamp_trade_usdc_micro,
    load_cex_dex_cost_params,
    net_spread_bps_after_costs,
)
from src.strategies.cex_dex_strategy import evaluate_cex_dex_opportunity
from src.utils.ai import AiApproval, get_ai_approval

logger = logging.getLogger(__name__)


@dataclass
class CycleContext:
    cex_mid: float
    dex_mid: float
    gross_bps: float
    net_bps: float
    direction: str
    size_usdc_micro: int


class CexDexCycle:
    def __init__(
        self,
        jupiter: JupiterExecutor | None = None,
        cex: CexExecutor | None = None,
    ) -> None:
        self.jupiter: JupiterExecutor = jupiter if jupiter is not None else get_jupiter_executor()
        self.cex: CexExecutor = cex if cex is not None else cex_executor
        if self.jupiter.keypair is not None:
            configure_jito(self.jupiter.client, self.jupiter.keypair)
        self.jito = JitoMultiRelay(
            client=self.jupiter.client,
            keypair=self.jupiter.keypair,
        )
        self._base_cost_bps, self._wdraw_bps, self._depth_util, self._max_impact = (
            load_cex_dex_cost_params()
        )
        self._probe_micro = settings.CEX_DEX_PROBE_USDC_MICRO
        self._poll_base = float(os.getenv("CEX_DEX_POLL_INTERVAL_SEC", "2.5"))

    @classmethod
    def create(cls) -> CexDexCycle:
        """Wire production ``JupiterExecutor`` + ``cex_executor`` after settings bootstrap."""
        jupiter = JupiterExecutor()
        if jupiter.keypair is not None:
            configure_jito(jupiter.client, jupiter.keypair)
        cycle = cls(jupiter=jupiter, cex=cex_executor)
        eff_net = cycle._effective_min_net_bps()
        logger.info(
            "CexDexCycle wired | jupiter=%s quote_only=%s signer=%s cex=%s rpc=%s",
            type(jupiter).__name__,
            jupiter.quote_only,
            signer_type(),
            type(cex_executor).__name__,
            (settings.SOLANA_RPC_URL or "")[:48],
        )
        logger.info(
            "CEX-DEX gates | min_gross=%s min_net_eff=%.1f (min_net=%s min_profit_bps=%s safety=%s) "
            "base_cost=%s probe_micro=%s near_miss_logs=%s",
            settings.CEX_DEX_MIN_GROSS_SPREAD_BPS,
            eff_net,
            settings.CEX_DEX_MIN_NET_SPREAD_BPS,
            settings.MIN_NET_PROFIT_BPS,
            settings.CEX_DEX_EDGE_SAFETY_BPS,
            settings.CEX_DEX_STRATEGY_BASE_COST_BPS,
            cycle._probe_micro,
            settings.CEX_DEX_LOG_NEAR_MISSES,
        )
        return cycle

    def _ai_confidence_floor(self) -> int:
        return max(
            settings.AI_APPROVE_MIN_CONFIDENCE,
            settings.CEX_DEX_AI_CONFIDENCE_FLOOR,
            cex_dex_ai_min_confidence(),
        )

    def _calculate_net_bps(
        self,
        gross_bps: float,
        direction: str,
        size_usdc_micro: int | None = None,
    ) -> float:
        return net_spread_bps_after_costs(
            gross_bps,
            size_usdc_micro or self._probe_micro,
            direction=direction,  # type: ignore[arg-type]
        )

    def _effective_min_net_bps(self) -> float:
        """Net edge threshold: ``CEX_DEX_MIN_NET_SPREAD_BPS`` (+ optional safety buffer)."""
        min_net_bps = float(settings.CEX_DEX_MIN_NET_SPREAD_BPS)
        legacy = float(os.getenv("MIN_NET_PROFIT_BPS", str(settings.MIN_NET_PROFIT_BPS)))
        if legacy > min_net_bps + 1:
            logger.warning(
                "MIN_NET_PROFIT_BPS=%.0f > CEX_DEX_MIN_NET_SPREAD_BPS=%s — "
                "CEX-DEX net gate uses min_net only; set MIN_NET_PROFIT_BPS=4 for AI alignment",
                legacy,
                settings.CEX_DEX_MIN_NET_SPREAD_BPS,
            )
        safety = float(
            os.getenv("CEX_DEX_EDGE_SAFETY_BPS", str(settings.CEX_DEX_EDGE_SAFETY_BPS))
        )
        return min_net_bps + max(0.0, safety)

    def _should_execute(
        self,
        ctx: CycleContext,
        *,
        cex_mid: float,
        jup_price: float,
    ) -> tuple[bool, float, float]:
        gross_bps = ctx.gross_bps
        net_bps = ctx.net_bps
        effective_need = self._effective_min_net_bps()

        logger.info(
            "CEX-DEX signal | gross=%.1fbps net=%.1fbps need=%.1fbps dir=%s cex=%.4f jup=%.4f",
            gross_bps,
            net_bps,
            effective_need,
            ctx.direction,
            cex_mid,
            jup_price,
        )

        if net_bps >= effective_need:
            logger.info(
                "CEX-DEX EXECUTE_CANDIDATE | gross=%.1f net=%.1f need=%.1f dir=%s",
                gross_bps,
                net_bps,
                effective_need,
                ctx.direction,
            )
            return True, gross_bps, net_bps

        if settings.CEX_DEX_LOG_NEAR_MISSES:
            logger.info(
                "CEX-DEX NEAR_MISS | net_gate gross=%.1f net=%.1f need=%.1f gap=%.1f "
                "dir=%s cex=%.4f jup=%.4f",
                gross_bps,
                net_bps,
                effective_need,
                effective_need - net_bps,
                ctx.direction,
                cex_mid,
                jup_price,
            )
            record_cex_dex_near_miss(gross_bps)
        else:
            logger.debug(
                "CEX-DEX below min edge | gross_bps=%.1f net_bps=%.1f need=%.1f",
                gross_bps,
                net_bps,
                effective_need,
            )
        return False, gross_bps, net_bps

    async def run_once(self) -> bool:
        if not check_global_safety() or circuit_breaker.should_pause():
            return False

        probe_size = min(settings.CEX_DEX_MAX_TRADE_USDC_MICRO, self._probe_micro)
        can, reason = await safety_store.can_trade(probe_size)
        if not can:
            logger.debug("CEX-DEX blocked by safety: %s", reason)
            return False

        cex_mid = await self.cex.get_mid_price("SOL_USDC")
        jup_price, quote = await self.jupiter.get_implied_usdc_per_sol(self._probe_micro)
        if not cex_mid or not jup_price or not quote:
            logger.debug(
                "CEX-DEX oracle incomplete | cex_mid=%s jup_price=%s quote=%s",
                cex_mid,
                jup_price,
                bool(quote),
            )
            return False

        logger.info(
            "CEX-DEX oracle | cex_mid=%.4f jup=%.4f USDC/SOL (executors: %s + %s)",
            cex_mid,
            jup_price,
            type(self.cex).__name__,
            type(self.jupiter).__name__,
        )

        spread = analyze_cex_dex_spread(cex_mid, jup_price)
        if not spread:
            return False

        gross_bps = spread.spread_bps_abs
        direction = spread.direction

        # Gross-spread gate (+ NEAR_MISS logs) before net / AI / sim / execute
        if not evaluate_cex_dex_opportunity(
            cex_mid, jup_price, probe_size, direction=direction
        ):
            note_cex_dex_context({"active": False, "gross_bps": gross_bps})
            return False

        size_usdc = self._calc_size(gross_bps, cex_mid)
        if size_usdc < settings.CEX_DEX_MIN_TRADE_USDC_MICRO:
            return False

        net_bps = self._calculate_net_bps(gross_bps, direction, size_usdc_micro=size_usdc)
        probe_ctx = CycleContext(
            cex_mid=cex_mid,
            dex_mid=jup_price,
            gross_bps=gross_bps,
            net_bps=net_bps,
            direction=direction,
            size_usdc_micro=size_usdc,
        )
        should_exec, gross_bps, net_bps = self._should_execute(
            probe_ctx, cex_mid=cex_mid, jup_price=jup_price
        )
        if not should_exec:
            note_cex_dex_context({"active": False, "gross_bps": gross_bps, "net_bps": net_bps})
            return False

        approval = await get_ai_approval(
            signal_type="cex_dex",
            gross_bps=gross_bps,
            cex_mid=cex_mid,
            jup_price=jup_price,
            size_usdc_micro=size_usdc,
            net_bps=net_bps,
            direction=direction,
        )
        if not approval.approve or approval.confidence < self._ai_confidence_floor():
            logger.info(
                "AI rejected CEX-DEX | conf=%s need=%s reason=%s",
                approval.confidence,
                self._ai_confidence_floor(),
                approval.reason,
            )
            return False

        ctx = CycleContext(
            cex_mid=cex_mid,
            dex_mid=jup_price,
            gross_bps=gross_bps,
            net_bps=net_bps,
            direction=direction,
            size_usdc_micro=size_usdc,
        )

        if not await self._simulate(ctx):
            return False

        success = await self._execute(ctx, approval)
        if success:
            safety_store.record_trade(size_usdc)
            note_cex_dex_context(
                {"active": True, "gross_bps": gross_bps, "size_usdc_micro": size_usdc}
            )
        return success

    def _calc_size(self, gross_bps: float, cex_mid: float) -> int:
        util = settings.CEX_DEX_DEPTH_UTILIZATION
        edge_scale = min(1.0, float(gross_bps) / 200.0)
        raw = int(settings.CEX_DEX_MAX_TRADE_USDC_MICRO * util * edge_scale)
        flash_cap = int(os.getenv("CEX_DEX_FLASH_AMOUNT_USDC_MICRO", "50000000"))
        liq_cap = int(50.0 * cex_mid * util * 1_000_000)
        capped = clamp_trade_usdc_micro(
            max_trade_usdc_micro=settings.CEX_DEX_MAX_TRADE_USDC_MICRO,
            flash_cap_usdc_micro=flash_cap,
            liquidity_cap_usdc_micro=liq_cap,
            min_trade_usdc_micro=settings.CEX_DEX_MIN_TRADE_USDC_MICRO,
        )
        return min(raw, capped) if raw > 0 else capped

    async def _simulate(self, ctx: CycleContext) -> bool:
        if not await self.jupiter.has_signing():
            logger.warning(
                "CEX-DEX sim skipped: no signing (quote-only or Ledger unavailable)"
            )
            return settings.TEST_MODE

        tx = await build_cex_dex_flash_tx(
            ctx.cex_mid,
            ctx.dex_mid,
            ctx.size_usdc_micro,
            client=self.jupiter.client,
            keypair=self.jupiter.keypair,
            jupiter=self.jupiter,
            direction=ctx.direction,
        )
        if tx is None:
            return False
        if await simulate(self.jupiter.client, tx):
            wallet_safety.record_successful_simulation()
            return True
        return False

    async def _execute(self, ctx: CycleContext, approval: AiApproval) -> bool:
        if settings.TEST_MODE:
            logger.info(
                "[TEST] CEX-DEX would execute %.2f USDC | dir=%s conf=%s",
                ctx.size_usdc_micro / 1e6,
                ctx.direction,
                approval.confidence,
            )
            return True

        if not settings.live_trading_confirm_enabled:
            logger.warning("LIVE_TRADING_CONFIRM not set — skipping live send")
            return False

        if not await self.jupiter.has_signing():
            logger.error(
                "Cannot execute: no signing keypair (SIGNER_TYPE=hot + PRIVATE_KEY_FILE)"
            )
            return False

        ok, reason = wallet_safety.before_live_send(ctx.size_usdc_micro)
        if not ok:
            logger.warning("Wallet safety blocked: %s", reason)
            return False

        if ctx.direction == "cex_cheap":
            return await self._execute_hybrid(ctx)

        return await self._execute_atomic(ctx)

    async def _execute_atomic(self, ctx: CycleContext) -> bool:
        tx = await build_cex_dex_flash_tx(
            ctx.cex_mid,
            ctx.dex_mid,
            ctx.size_usdc_micro,
            client=self.jupiter.client,
            keypair=self.jupiter.keypair,
            jupiter=self.jupiter,
            direction=ctx.direction,
        )
        if tx is None:
            return False

        tip = self._calc_tip(ctx)
        if self.jupiter.keypair is None:
            return False
        bundle_id = await self.jito.send_bundle([tx], tip, append_tip_tx=True)
        if bundle_id:
            wallet_safety.record_live_trade_usdc_micro(ctx.size_usdc_micro)
            logger.info("CEX-DEX atomic bundle sent | %s tip=%s", bundle_id, tip)
            return True
        return False

    async def _execute_hybrid(self, ctx: CycleContext) -> bool:
        """CEX buy SOL → withdraw buffer → on-chain Kamino/Jupiter flash bundle."""
        order = await self.cex.buy_sol(ctx.size_usdc_micro, price=ctx.cex_mid)
        if not order and not settings.TEST_MODE:
            return False

        await asyncio.sleep(settings.CEX_WITHDRAWAL_BUFFER_SEC)
        return await self._execute_atomic(ctx)

    def _calc_tip(self, ctx: CycleContext) -> int:
        est_profit_usdc = (ctx.size_usdc_micro / 1_000_000) * (ctx.gross_bps / 10000.0)
        sol_px = max(1.0, ctx.cex_mid)
        tip = int(est_profit_usdc * settings.DYNAMIC_TIP_MULTIPLIER * 1_000_000_000 / sol_px)
        lo = int(os.getenv("JITO_TIP_LAMPORTS_MIN", "50000"))
        hi = settings.MAX_TIP_LAMPORTS
        return max(lo, min(hi, tip))

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
                delay = self._poll_base * (0.85 + random.random() * 0.3)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("CEX-DEX cycle error: %s", exc, exc_info=True)
                err = str(exc).lower()
                if "429" in err or "rate limit" in err:
                    await asyncio.sleep(30)
                else:
                    await asyncio.sleep(10)


_cycle_singleton: CexDexCycle | None = None


def get_cex_dex_cycle() -> CexDexCycle:
    """Lazy singleton — call after ``bootstrap_config()``."""
    global _cycle_singleton
    if _cycle_singleton is None:
        _cycle_singleton = CexDexCycle.create()
    return _cycle_singleton


class _CexDexCycleProxy:
    """Defer executor wiring until first use (after env bootstrap)."""

    def __getattr__(self, name: str):
        return getattr(get_cex_dex_cycle(), name)


cex_dex_cycle = _CexDexCycleProxy()


def get_cex_executor() -> CexExecutor:
    """Shared Backpack executor for ``cex_dex_arb`` and cycle."""
    return cex_executor


def get_jupiter_executor_for_cycle() -> JupiterExecutor:
    """Re-export for strategies that import executors from this module."""
    return get_jupiter_executor()
