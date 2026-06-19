# src/strategies/cex_dex_core.py
"""
Core CEX-DEX signal engine — multi-layer gate (CEX + DEX + vol + inventory).
Shared spread helpers used by flash/cycle/arb modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

from src.ai.decision_engine import AIScorer, get_ai_scorer
from src.cex.backpack import BackpackClient
from src.config.settings import Settings, get_settings
from src.core.circuit_breaker import circuit_breaker
from src.core.risk import RiskEngine
from src.core.wallet import check_global_safety, get_sol_balance
from src.dex.jupiter import JupiterClient, get_jupiter_executor
from src.monitoring.metrics import record_trade_signal
from src.strategies.brain_pnl import append_realized_pnl_usd, bump_min_confidence_for_recent_pnl
from src.utils.price import bps_diff

logger = logging.getLogger(__name__)

TradeDirection = Literal["cex_cheap", "dex_cheap"]
Direction = TradeDirection  # legacy alias for cex_dex.py


@dataclass(frozen=True)
class CexDexSpreadAnalysis:
    cex_mid: float
    dex_mid: float
    direction: TradeDirection
    spread_bps_abs: float
    spread_pct_signed: float


class CexDexOpportunity:
    """Single scan result (sizes in USDC micro-units unless noted)."""

    def __init__(
        self,
        gross_bps: int,
        net_bps: int,
        size_usdc: int,
        confidence: float,
        *,
        cex_price: float = 0.0,
        jupiter_price: float = 0.0,
        expected_profit_usdc: float = 0.0,
        reason: str = "strong_spread_high_confidence",
    ) -> None:
        self.gross_bps = gross_bps
        self.net_bps = net_bps
        self.size_usdc = size_usdc
        self.size_usdc_micro = size_usdc
        self.confidence = confidence
        self.cex_price = cex_price
        self.jupiter_price = jupiter_price
        self.expected_profit_usdc = expected_profit_usdc
        self.reason = reason


def load_cex_dex_cost_params() -> tuple[int, int, float, float]:
    """(base_cost_bps, withdrawal_latency_bps, depth_utilization, max_impact_pct)."""
    s = get_settings()
    base = int(os.getenv("CEX_DEX_STRATEGY_BASE_COST_BPS", str(s.CEX_DEX_STRATEGY_BASE_COST_BPS)))
    wdraw = int(os.getenv("CEX_DEX_WITHDRAWAL_LATENCY_BPS", str(s.CEX_DEX_WITHDRAWAL_LATENCY_BPS)))
    util = float(
        os.getenv("CEX_DEX_DEPTH_UTILIZATION", str(s.trading.cex_dex_depth_utilization))
    )
    impact = float(os.getenv("FLASH_SIZE_IMPACT_TOLERANCE_PCT", str(s.FLASH_SIZE_IMPACT_TOLERANCE_PCT)))
    return base, wdraw, util, impact


def analyze_cex_dex_spread(cex_mid: float, dex_mid: float) -> CexDexSpreadAnalysis | None:
    if cex_mid <= 0 or dex_mid <= 0:
        return None
    signed_bps = float(bps_diff(cex_mid, dex_mid))
    spread_bps_abs = abs(signed_bps)
    spread_pct_signed = signed_bps / 100.0
    direction: TradeDirection = "cex_cheap" if dex_mid > cex_mid else "dex_cheap"
    return CexDexSpreadAnalysis(
        cex_mid=float(cex_mid),
        dex_mid=float(dex_mid),
        direction=direction,
        spread_bps_abs=spread_bps_abs,
        spread_pct_signed=spread_pct_signed,
    )


def resolve_direction(
    direction: str | None,
    cex_mid: float,
    jup_price: float,
) -> TradeDirection | None:
    if direction in ("cex_cheap", "dex_cheap"):
        return direction  # type: ignore[return-value]
    analysis = analyze_cex_dex_spread(cex_mid, jup_price)
    return analysis.direction if analysis else None


def set_cex_cheap_flags(opportunity: dict[str, Any], direction: str | None = None) -> None:
    """Set ``is_cex_cheap`` on a scan/execution opportunity dict."""
    if direction is not None:
        opportunity["direction"] = direction
    elif "direction" not in opportunity and opportunity.get("cex_price") and opportunity.get(
        "jup_price"
    ):
        spread = analyze_cex_dex_spread(
            float(opportunity["cex_price"]),
            float(opportunity["jup_price"]),
        )
        if spread:
            opportunity["direction"] = spread.direction
    d = opportunity.get("direction")
    opportunity["is_cex_cheap"] = d == "cex_cheap"


def gate_cex_dex_direction(opportunity: dict[str, Any]) -> dict[str, str] | None:
    """
    Plan 4 — direction alignment (CEX buy → DEX sell requires CEX cheap).

    In DEX-cheap regime, strong forward signals may pass via ``DirectionAwareBrain``
    (soft deprioritize, not hard block).
    """
    set_cex_cheap_flags(opportunity)
    if opportunity.get("is_cex_cheap", False):
        return None

    from src.strategies.direction_aware_brain import DirectionAwareBrain

    if DirectionAwareBrain().evaluate(opportunity):
        return None

    status = str(opportunity.get("block_reason") or "wrong_direction_dex_cheap")
    return {"status": status}


async def gate_cex_dex_ask_depth(
    backpack: BackpackClient,
    opportunity: dict[str, Any],
) -> dict[str, str] | None:
    """
    Plan 4 — CEX ask depth must cover the trade USDC notional (micro).

    Returns ``{"status": "insufficient_depth"}`` when the book is too thin.
    """
    size_micro = int(
        opportunity.get("size_usdc_micro") or opportunity.get("size_usdc") or 0
    )
    if size_micro <= 0:
        return None

    symbol = str(
        opportunity.get("symbol")
        or opportunity.get("backpack_symbol")
        or "SOL"
    )
    depth_ok = await backpack.check_ask_depth(
        symbol=symbol,
        required_usdc=size_micro,
    )
    if not depth_ok:
        return {"status": "insufficient_depth"}
    return None


async def gate_cex_dex_detection_alignment(
    backpack: BackpackClient,
    opportunity: dict[str, Any],
    *,
    check_depth: bool = True,
) -> dict[str, str] | None:
    """Direction gate, then optional ask-depth gate."""
    reject = gate_cex_dex_direction(opportunity)
    if reject:
        return reject
    if check_depth:
        return await gate_cex_dex_ask_depth(backpack, opportunity)
    return None


def _use_component_cost_model() -> bool:
    raw = (os.getenv("CEX_DEX_USE_COMPONENT_COST_MODEL") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def modeled_roundtrip_cost_bps(
    size_usdc_micro: int,
    *,
    volatility_bps: float = 0.0,
) -> float:
    """
    All-in modeled drag (bps) for a trade size.

    Default uses ``CEX_DEX_STRATEGY_BASE_COST_BPS`` only (live logs showed the
    legacy stacked fee fields summed ~92 bps and zeroed every net estimate).
    Set ``CEX_DEX_USE_COMPONENT_COST_MODEL=true`` to restore per-leg stacking.
    """
    cfg = get_settings()
    if _use_component_cost_model():
        base_cost = float(
            cfg.CEX_DEX_CEX_FEE_ROUNDTRIP_BPS
            + cfg.CEX_DEX_JUPITER_LEG_FEE_BUFFER_BPS
            + cfg.CEX_DEX_EXECUTION_SLIPPAGE_BUFFER_BPS
            + cfg.CEX_DEX_WITHDRAWAL_LATENCY_BPS
        )
    else:
        from src.strategies.cex_dex_roundtrip import get_effective_cost_bps

        base_cost = get_effective_cost_bps(size_usdc_micro)

    size_usdc = max(0.0, size_usdc_micro / 1_000_000.0)
    cap = max(1.0, float(cfg.MAX_FLASH_USDC))
    max_impact = float(os.getenv("CEX_DEX_MAX_SIZE_IMPACT_BPS", "6"))
    impact_slope = float(os.getenv("CEX_DEX_SIZE_IMPACT_SLOPE_BPS", "6"))
    ref_usdc = max(1.0, float(os.getenv("CEX_DEX_SIZE_IMPACT_REF_USDC", "20")))
    try:
        impact_exp = float(os.getenv("CEX_DEX_SIZE_IMPACT_EXPONENT", "1.0"))
    except (TypeError, ValueError):
        impact_exp = 1.0
    size_ratio = size_usdc / ref_usdc
    impact_bps = min(max_impact, impact_slope * (size_ratio**impact_exp))
    vol_factor = float(os.getenv("CEX_DEX_VOLATILITY_COST_FACTOR", "0.05"))
    vol_bps = max(0.0, float(volatility_bps) * vol_factor)
    return base_cost + impact_bps + vol_bps


def net_spread_bps_after_costs(
    gross_bps: float,
    size_usdc_micro: int,
    *,
    direction: TradeDirection | str = "dex_cheap",
    volatility_bps: float = 0.0,
) -> float:
    _ = direction
    drag = modeled_roundtrip_cost_bps(
        size_usdc_micro,
        volatility_bps=volatility_bps,
    )
    return float(gross_bps) - drag


def dynamic_min_trade_usdc_micro(
    gross_bps: float,
    *,
    settings: Settings | None = None,
) -> int:
    """Floor trade size in micro-USDC; scales with gross edge using env-tunable slope/cap."""
    cfg = settings or get_settings()
    base = int(cfg.CEX_DEX_MIN_TRADE_USDC_MICRO)
    try:
        per_bps_micro = int(float(os.getenv("CEX_DEX_DYNAMIC_MIN_TRADE_PER_BPS_MICRO", "600000")))
    except (TypeError, ValueError):
        per_bps_micro = 600_000
    scaled = int(abs(float(gross_bps)) * max(1, per_bps_micro))
    cap_raw = (os.getenv("CEX_DEX_DYNAMIC_MIN_TRADE_CAP_MICRO") or "").strip()
    if cap_raw:
        try:
            cap = max(base, int(cap_raw))
            scaled = min(scaled, cap)
        except ValueError:
            pass
    return max(base, scaled)


def clamp_trade_usdc_micro(
    *,
    max_trade_usdc_micro: int,
    flash_cap_usdc_micro: int,
    liquidity_cap_usdc_micro: int,
    min_trade_usdc_micro: int,
) -> int:
    cap = min(max_trade_usdc_micro, flash_cap_usdc_micro, liquidity_cap_usdc_micro)
    return max(min_trade_usdc_micro, cap)


def cex_dex_ai_min_confidence() -> int:
    cfg = get_settings()
    base = max(
        int(cfg.trading.ai_approve_min_confidence),
        int(cfg.trading.cex_dex_ai_confidence_floor),
        cfg.AI_APPROVE_MIN_CONFIDENCE,
        cfg.CEX_DEX_AI_CONFIDENCE_FLOOR,
    )
    return bump_min_confidence_for_recent_pnl(base)


def check_daily_loss_limit() -> bool:
    if not check_global_safety():
        return True
    return circuit_breaker.should_pause()


def record_trade(strategy: str, profit_usdc: float, net_bps: float) -> None:
    _ = strategy
    append_realized_pnl_usd(float(profit_usdc))
    circuit_breaker.record_trade(float(profit_usdc))
    logger.info("cex_dex trade recorded | profit=$%.2f net=%.1fbps", profit_usdc, net_bps)


def inc_trade_counter(outcome: str) -> None:
    logger.info("cex_dex trade outcome=%s", outcome)


class CexDexStrategy:
    """Multi-layer CEX-DEX scanner (CEX + DEX + volatility + inventory)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        backpack: BackpackClient | None = None,
        jupiter: JupiterClient | None = None,
        risk: RiskEngine | None = None,
        ai: AIScorer | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.backpack = backpack or BackpackClient()
        self.jupiter = jupiter or get_jupiter_executor()
        self.risk = risk or RiskEngine(self.settings)
        self.ai = ai or get_ai_scorer()

    async def get_opportunity(self) -> CexDexOpportunity | None:
        """Multi-layer signal: CEX + DEX + volatility + inventory."""
        try:
            if not self.risk.can_trade(0):
                return None

            inventory_sol = await get_sol_balance()
            if not self.risk.within_inventory_limit(float(inventory_sol or 0.0)):
                return None

            cex_price = await self.backpack.get_sol_usdc_price()
            jup_quote = await self.jupiter.get_quote(amount=100_000_000)
            if not cex_price or not jup_quote:
                return None

            gross_bps = int((cex_price - jup_quote.price) / jup_quote.price * 10000)
            estimated_cost_bps = int(
                self.settings.trading.cex_dex_strategy_base_cost_bps
                or self.settings.CEX_DEX_STRATEGY_BASE_COST_BPS
                or 62
            )
            net_bps = gross_bps - estimated_cost_bps

            vol = await self._get_1min_volatility()
            if vol > self.settings.trading.volatility_filter_bps:
                logger.info("High vol skipped: %sbps", vol)
                return None

            size_usdc = await self._calculate_optimal_size(net_bps, cex_price)
            confidence = await self._get_hybrid_confidence(gross_bps, net_bps, size_usdc)

            min_net = int(self.settings.trading.cex_dex_min_net_spread_bps)
            min_conf = float(self.settings.trading.ai_approve_min_confidence)
            if net_bps >= min_net and confidence >= min_conf:
                profit_usdc = (net_bps / 10000.0) * (size_usdc / 1_000_000.0)
                opp = CexDexOpportunity(
                    gross_bps,
                    net_bps,
                    size_usdc,
                    confidence,
                    cex_price=float(cex_price),
                    jupiter_price=float(jup_quote.price),
                    expected_profit_usdc=profit_usdc,
                )
                record_trade_signal("cex_dex", float(gross_bps), float(net_bps))
                return opp
            return None
        except Exception as exc:
            logger.error("Opportunity check failed: %s", exc, exc_info=True)
            return None

    async def scan(self) -> CexDexOpportunity | None:
        """Alias for ``get_opportunity`` (legacy callers)."""
        return await self.get_opportunity()

    async def _calculate_optimal_size(self, net_bps: int, cex_price: float) -> int:
        """Sweet spot 30k–500k USDC (micro) with utilization + edge scaling."""
        t = self.settings.trading
        base = int(t.max_flash_usdc * t.flash_size_utilization * 1_000_000)
        edge_factor = min(1.0, net_bps / 120.0)
        size = int(base * edge_factor)
        min_micro = int(t.min_flash_usdc * 1_000_000)
        max_micro = int(t.max_flash_usdc * 1_000_000)
        size = max(min_micro, min(max_micro, size))

        capped = clamp_trade_usdc_micro(
            max_trade_usdc_micro=self.settings.CEX_DEX_MAX_TRADE_USDC_MICRO,
            flash_cap_usdc_micro=max_micro,
            liquidity_cap_usdc_micro=int(50.0 * cex_price * 1_000_000),
            min_trade_usdc_micro=min_micro,
        )
        return min(size, capped)

    async def _get_1min_volatility(self) -> int:
        """1m volatility proxy (CEX feed); falls back to env default."""
        try:
            from src.cex.price_feed import cex_feed

            _, vol = await cex_feed.get_price_and_volatility_bps("SOL/USDC")
            return int(vol or 85)
        except Exception:
            return int(os.getenv("DEFAULT_VOLATILITY_BPS", "85"))

    async def _get_hybrid_confidence(self, gross: int, net: int, size: int) -> float:
        heuristic = min(95.0, 60.0 + (net * 0.8))
        floor = max(
            float(self.settings.trading.cex_dex_ai_confidence_floor),
            float(cex_dex_ai_min_confidence()),
        )
        if heuristic >= floor - 4.0:
            try:
                ai_score = await self.ai.score(
                    cex_price=0.0,
                    jup_price=0.0,
                    size_usdc=size / 1_000_000,
                    net_bps=float(net),
                )
                return max(heuristic, float(ai_score))
            except Exception as exc:
                logger.debug("AI score fallback to heuristic: %s", exc)
        return heuristic

    async def execute(self, opp: CexDexOpportunity) -> bool:
        """CEX buy → buffer → Jupiter/Jito swap."""
        try:
            logger.info(
                "Executing CEX-DEX | size=$%.1fk net=%dbps ai=%.1f%%",
                opp.size_usdc / 1_000_000,
                opp.net_bps,
                opp.confidence,
            )
            buy_ok = await self.backpack.execute_market_buy(opp.size_usdc)
            if not buy_ok:
                return False

            await asyncio.sleep(self.settings.CEX_WITHDRAWAL_BUFFER_SEC)
            from src.core.jito_tip import mev_protection_enabled, resolve_jito_tip_for_trade

            tip = self.settings.CEX_DEX_JITO_TIP_LAMPORTS
            if mev_protection_enabled():
                tip = resolve_jito_tip_for_trade(
                    float(opp.net_bps),
                    float(opp.gross_bps),
                    int(opp.size_usdc),
                    confidence=float(opp.confidence),
                )
            swap = await self.jupiter.execute_swap_with_jito(
                amount_micro=opp.size_usdc,
                tip_lamports=tip,
                net_bps=float(opp.net_bps),
                gross_bps=float(opp.gross_bps),
                confidence=float(opp.confidence),
            )
            if swap.get("success"):
                record_trade("cex_dex", opp.expected_profit_usdc, float(opp.net_bps))
                self.risk.record_trade_result(opp.expected_profit_usdc)
                await self.ai.record_trade_result(
                    opp.expected_profit_usdc, float(opp.net_bps), opp.confidence
                )
                inc_trade_counter("win")
                return True
        except Exception as exc:
            logger.error("Execution failed: %s", exc, exc_info=True)
            inc_trade_counter("loss")
        return False


# Legacy name used by older entrypoints
CexDexCore = CexDexStrategy
