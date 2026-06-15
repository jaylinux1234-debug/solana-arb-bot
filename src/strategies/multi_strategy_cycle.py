"""Unified cycle: brain snapshots → priority pick → cex_dex / collateral / liquidation."""

from __future__ import annotations

import logging
import os
from typing import Any

from src.config.settings import Settings
from src.core.circuit_breaker import circuit_breaker
from src.core.rpc_urls import resolve_rpc_url
from src.strategies.brain_signals import (
    brain_snapshot,
    dex_cex_reverse_signal_present,
    get_backrun_context,
    note_backrun_context,
    note_cex_dex_context,
    note_collateral_best,
    note_dex_cex_reverse_context,
    reset_cycle_signals,
)
from src.strategies.cex_dex_core import analyze_cex_dex_spread, net_spread_bps_after_costs
from src.strategies.cex_dex_strategy import CexDexStrategy
from src.strategies.brain import StrategyBrain
from src.utils.ai import score_strategies_cycle

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def refresh_dex_cex_reverse_brain_snapshot(strategy: CexDexStrategy) -> None:
    """Publish dex_cheap reverse-lane snapshot for the cycle brain."""
    if not _env_bool("ENABLE_DEX_CEX_REVERSE", True):
        note_dex_cex_reverse_context({"active": False})
        return
    try:
        from src.strategies.dex_cex_reverse import DexCexReverseStrategy

        reverse = DexCexReverseStrategy(
            jupiter_executor=strategy.jupiter,
            backpack_client=strategy.backpack,
            wallet_pubkey=strategy.settings.wallet_pubkey or strategy.settings.WALLET_PUBKEY,
            settings=strategy.settings,
            risk=strategy.risk,
        )
        opp = await reverse.scan_dex_cheap()
        if not opp:
            note_dex_cex_reverse_context({"active": False, "direction": "dex_cheap"})
            return
        min_gross = float(os.getenv("DEX_CEX_REVERSE_MIN_GROSS_BPS", "10"))
        active = float(opp.get("gross_bps") or 0) >= min_gross
        note_dex_cex_reverse_context(
            {
                "active": active,
                "is_dex_cheap": True,
                "direction": "dex_cheap",
                "gross_bps": opp.get("gross_bps"),
                "net_bps": opp.get("net_bps"),
                "pair": opp.get("pair_label"),
            }
        )
    except Exception as exc:
        logger.debug("DEX-CEX reverse brain snapshot failed: %s", exc)
        note_dex_cex_reverse_context({"active": False})


async def refresh_cex_dex_brain_snapshot(strategy: CexDexStrategy) -> None:
    """Probe primary pair and publish gross/net for the cycle brain."""
    try:
        pair = strategy._pairs[0] if strategy._pairs else None
        if pair is None:
            note_cex_dex_context({"active": False})
            return

        cex_buy, cex_mid, cex_ask = await strategy.backpack.get_cex_buy_reference_price(
            pair.backpack_symbol
        )
        probe_micro = strategy._probe_usdc_micro()
        sell_px, _ = await strategy.jupiter.get_implied_usdc_per_base_sell(
            probe_micro,
            pair.base_mint,
            float(cex_buy),
            base_decimals=pair.base_decimals,
        )
        jup_price = float(sell_px) if sell_px and sell_px > 0 else None
        if jup_price is None:
            jup_price, _ = await strategy.jupiter.get_implied_usdc_per_base(
                probe_micro,
                pair.base_mint,
                base_decimals=pair.base_decimals,
            )
        if pair.symbol == "SOL" and jup_price and jup_price > 0:
            from src.dex.executor import get_dex_executor

            dex_q = await get_dex_executor().get_best_dex_price(
                probe_micro,
                use_phoenix=True,
                jupiter_price=float(jup_price),
            )
            if dex_q and dex_q.price > 0:
                jup_price = float(dex_q.price)
        if not cex_buy or not jup_price:
            note_cex_dex_context({"active": False})
            return

        from src.strategies.volatility_gate import record_cex_price

        record_cex_price(float(cex_buy))

        spread = analyze_cex_dex_spread(float(cex_buy), float(jup_price))
        if spread is None:
            note_cex_dex_context({"active": False, "gross_bps": 0.0})
            return

        from src.utils.price import bps_diff

        gross_bps = abs(float(bps_diff(cex_buy, jup_price)))
        size_micro = strategy._probe_usdc_micro()
        net_bps = net_spread_bps_after_costs(
            gross_bps,
            size_micro,
            direction=spread.direction,  # type: ignore[arg-type]
        )
        min_gross = float(strategy.settings.CEX_DEX_MIN_GROSS_SPREAD_BPS)
        min_net = float(strategy.settings.CEX_DEX_MIN_NET_SPREAD_BPS)
        active = (
            gross_bps >= min_gross
            and net_bps >= min_net
            and spread.direction == "cex_cheap"
        )
        note_cex_dex_context(
            {
                "active": active,
                "gross_bps": gross_bps,
                "spread_bps_gross": gross_bps,
                "net_bps": net_bps,
                "spread_bps_net": net_bps,
                "pair": pair.pair_label,
                "direction": spread.direction,
                "cex_mid": cex_mid,
                "cex_ask": cex_ask,
                "meets_gross_gate": gross_bps >= min_gross,
                "meets_net_gate": net_bps >= min_net,
            }
        )
    except Exception as exc:
        logger.debug("CEX-DEX brain snapshot failed: %s", exc)
        note_cex_dex_context({"active": False})


async def refresh_collateral_brain_snapshot(strategy: CexDexStrategy) -> None:
    if not _env_bool("ENABLE_COLLATERAL_RATE_ARB", True):
        note_collateral_best({"active": False, "spread_bps": 0.0})
        return

    try:
        from src.strategies.collateral_executor import get_collateral_executor

        executor = get_collateral_executor(strategy.settings)
        opps = await executor.find_opportunity()
        if not opps:
            note_collateral_best({"active": False, "spread_bps": 0.0, "net_bps": 0.0})
            return
        top = opps[0]
        net_bps = float(top.get("net_bps") or top.get("spread_bps") or 0)
        min_net = float(
            os.getenv(
                "COLLATERAL_MIN_NET_BPS",
                os.getenv("COLLATERAL_MIN_SPREAD_BPS", "35"),
            )
        )
        note_collateral_best(
            {
                **top,
                "active": net_bps >= min_net,
                "spread_bps": net_bps,
                "net_bps": net_bps,
            }
        )
        executor.log_scan(opps)
    except Exception as exc:
        logger.debug("Collateral brain snapshot failed: %s", exc)
        note_collateral_best({"active": False})


async def refresh_liquidation_brain_snapshot() -> None:
    if circuit_breaker.should_pause():
        return
    try:
        from src.strategies.liquidation_executor import get_liquidation_executor

        await get_liquidation_executor().refresh_brain_snapshot()
    except Exception as exc:
        logger.debug("Liquidation brain refresh failed: %s", exc)


def refresh_backrun_brain_snapshot() -> None:
    """Refresh backrun metadata without clearing an active webhook signal within TTL."""
    enabled = _env_bool("ENABLE_HELIUS_WEBHOOK_BACKRUN", False)
    existing = get_backrun_context()
    if existing.get("active") is True:
        note_backrun_context(
            {
                **existing,
                "enabled": enabled,
                "pipeline_active": enabled,
            }
        )
        return
    note_backrun_context(
        {
            "enabled": enabled,
            "pipeline_active": enabled,
            "active": False,
            "amount_micro": 0,
        }
    )


_last_cycle_ctx: dict[str, object] = {}


def get_last_cycle_context() -> dict[str, object]:
    """Last unified-cycle dispatch metrics (for main-loop logging)."""
    return dict(_last_cycle_ctx)


def _cycle_spreads_from_snapshot(snapshot: dict) -> tuple[float, float]:
    from src.strategies.brain_signals import cex_dex_gross_bps_from_snapshot

    cx = snapshot.get("cex_dex") if isinstance(snapshot.get("cex_dex"), dict) else {}
    gross = float(cex_dex_gross_bps_from_snapshot(snapshot) or 0.0)
    try:
        net = float(cx.get("spread_bps_net") or cx.get("net_bps") or 0.0)
    except (TypeError, ValueError):
        net = 0.0
    return gross, net


def _pick_mev_lane(snapshot: dict) -> str | None:
    from src.strategies.brain_signals import (
        backrun_signal_present,
        collateral_signal_present,
        lane_signal_present,
    )

    order = [
        s.strip()
        for s in os.getenv(
            "STRATEGY_PRIORITY_ORDER",
            "backrun,collateral_swap,liquidation",
        ).split(",")
        if s.strip()
    ]
    for lane in order:
        if lane == "backrun" and backrun_signal_present(snapshot):
            return "backrun"
        if lane == "collateral_swap" and collateral_signal_present(snapshot):
            return "collateral_swap"
        if lane == "liquidation" and lane_signal_present(snapshot, "liquidation"):
            return "liquidation"
    if collateral_signal_present(snapshot):
        return "collateral_swap"
    if backrun_signal_present(snapshot):
        return "backrun"
    if lane_signal_present(snapshot, "liquidation"):
        return "liquidation"
    return None


async def run_mev_router_cycle(
    strategy: CexDexStrategy,
    settings: Settings,
) -> bool:
    """
    MEV-only unified cycle for v2 hybrid (skips cex_dex + dex_cex_reverse execution).
    """
    reset_cycle_signals()
    await refresh_collateral_brain_snapshot(strategy)
    refresh_backrun_brain_snapshot()
    await refresh_liquidation_brain_snapshot()

    snapshot = brain_snapshot()
    picked = _pick_mev_lane(snapshot)
    global _last_cycle_ctx
    gross_bps, net_bps = _cycle_spreads_from_snapshot(snapshot)
    if not picked:
        _last_cycle_ctx = {"lane": "mev_idle", "gross_bps": gross_bps, "net_bps": net_bps}
        return False

    _last_cycle_ctx = {
        "lane": picked,
        "gross_bps": gross_bps,
        "net_bps": net_bps,
        "brain_conf": 0,
    }
    logger.info(
        "MEV router | executing_lane=%s gross=%.1f net=%.1f",
        picked,
        gross_bps,
        net_bps,
    )

    if picked in ("collateral_swap", "liquidation", "backrun"):
        from src.strategies.mev_dispatch import execute_mev_lane

        return await execute_mev_lane(picked, snapshot, settings=settings)

    return False


async def run_unified_cycle(
    strategy: CexDexStrategy,
    settings: Settings,
) -> bool:
    """
    Refresh brain snapshots, score lanes, execute highest-priority viable strategy.

    Returns True if any lane attempted execution.
    """
    reset_cycle_signals()
    await refresh_cex_dex_brain_snapshot(strategy)
    await refresh_dex_cex_reverse_brain_snapshot(strategy)
    await refresh_collateral_brain_snapshot(strategy)
    refresh_backrun_brain_snapshot()
    await refresh_liquidation_brain_snapshot()

    snapshot = brain_snapshot()
    picked = "cex_dex"
    brain_conf = 0
    brain_source = "default"

    use_strategy_brain = _env_bool("ENABLE_STRATEGY_BRAIN_SELECTOR", True)
    if use_strategy_brain:
        try:
            strategy_brain = StrategyBrain(
                strategy.backpack,
                strategy.jupiter,
                settings=settings,
                wallet_pubkey=settings.wallet_pubkey or settings.WALLET_PUBKEY,
                risk=strategy.risk,
            )
            brain = await strategy_brain.select_from_snapshot(snapshot)
            picked = str(brain.get("strategy") or "cex_dex")
            brain_conf = min(100, int(float(brain.get("score") or 0)))
            brain_source = str(brain.get("source") or "strategy_brain")
            logger.info(
                "StrategyBrain | best=%s score=%s gates=%s scores=%s",
                picked,
                brain_conf,
                (brain.get("gates") or {}).get("mode"),
                brain.get("scores"),
            )
            try:
                from src.monitoring.brain_choice_log import append_brain_choice

                append_brain_choice(
                    {
                        "best_strategy": picked,
                        "confidence": brain_conf,
                        "scores": brain.get("scores"),
                        "gates": brain.get("gates"),
                        "source": brain_source,
                    }
                )
            except Exception as log_exc:
                logger.debug("brain choice log skipped: %s", log_exc)
        except Exception as exc:
            logger.warning("StrategyBrain selection failed: %s", exc)

    blend_ai = _env_bool("STRATEGY_BRAIN_BLEND_AI", False)
    if _env_bool("ENABLE_AI_CYCLE_BRAIN", settings.ENABLE_AI_CYCLE_BRAIN) and (
        not use_strategy_brain or blend_ai
    ):
        try:
            brain = await score_strategies_cycle(snapshot)
            ai_picked = str(brain.get("best_strategy") or "cex_dex")
            ai_conf = int(brain.get("confidence") or 0)
            if not use_strategy_brain or ai_conf > brain_conf:
                picked = ai_picked
                brain_conf = ai_conf
                brain_source = str(brain.get("source", "openai"))
            logger.info(
                "Cycle brain | best=%s confidence=%s scores=%s source=%s",
                picked,
                brain_conf,
                brain.get("scores"),
                brain_source,
            )
            try:
                from src.monitoring.brain_choice_log import append_brain_choice

                append_brain_choice(
                    {
                        "best_strategy": picked,
                        "confidence": brain_conf,
                        "scores": brain.get("scores"),
                        "source": brain_source,
                    }
                )
            except Exception as log_exc:
                logger.debug("brain choice log skipped: %s", log_exc)
        except Exception as exc:
            logger.warning("Cycle brain scoring failed: %s", exc)

    if picked == "none":
        picked = "cex_dex"

    # Default to CEX-DEX when no alternate lane has a real snapshot signal.
    from src.strategies.brain_signals import (
        backrun_signal_present,
        collateral_signal_present,
        lane_signal_present,
    )

    if picked == "backrun" and not backrun_signal_present(snapshot):
        picked = "cex_dex"
    elif picked == "collateral_swap" and not collateral_signal_present(snapshot):
        picked = "cex_dex"
    elif picked == "liquidation" and not lane_signal_present(snapshot, "liquidation"):
        picked = "cex_dex"
    elif (
        picked == "dex_cex_reverse"
        and not dex_cex_reverse_signal_present(snapshot)
        and not _env_bool("ENABLE_DIRECTION_AWARE_BRAIN", True)
    ):
        picked = "cex_dex"

    gross_bps, net_bps = _cycle_spreads_from_snapshot(snapshot)
    global _last_cycle_ctx
    _last_cycle_ctx = {
        "lane": picked,
        "gross_bps": gross_bps,
        "net_bps": net_bps,
        "brain_conf": brain_conf,
    }
    logger.info(
        "Unified cycle | executing_lane=%s brain_conf=%s gross=%.1f net=%.1f",
        picked,
        brain_conf,
        gross_bps,
        net_bps,
    )
    if net_bps > 0:
        logger.info(
            "ROUNDTRIP_PRE_SIM_OK | net_bps=%.1f gross=%.1f lane=%s",
            net_bps,
            gross_bps,
            picked,
        )

    if picked == "dex_cex_reverse" and _env_bool("ENABLE_DEX_CEX_REVERSE", True):
        if _env_bool("ENABLE_REVERSE_USDC_BOOTSTRAP", True):
            try:
                from src.strategies.reverse_bootstrap import maybe_bootstrap_usdc

                boot = await maybe_bootstrap_usdc(strategy.jupiter)
                if boot.get("status") == "ok":
                    logger.info("Reverse USDC bootstrap: %s", boot)
            except Exception as boot_exc:
                logger.debug("reverse bootstrap skipped: %s", boot_exc)
        if await _run_dex_cex_reverse_lane(strategy, snapshot, brain_conf):
            return True
        if _env_bool("SKIP_FORWARD_SCAN_WHEN_REVERSE_PICKED", True):
            logger.info(
                "SKIP_FORWARD_IN_DEX_CHEAP | executing_lane=dex_cex_reverse gross=%.1f net=%.1f",
                gross_bps,
                net_bps,
            )
            return False
        logger.debug("DEX-CEX reverse lane idle — continuing CEX-DEX scan")

    if picked == "collateral_swap" and _env_bool("ENABLE_COLLATERAL_RATE_ARB", True):
        return await _run_collateral_lane(strategy)

    if picked == "liquidation":
        await refresh_liquidation_brain_snapshot()
        if lane_signal_present(snapshot, "liquidation"):
            return False

    if picked == "backrun" and backrun_signal_present(snapshot):
        logger.debug("Backrun swap signal — webhook handles execution; continuing CEX-DEX scan")

    return await strategy.run_cycle()


async def _run_dex_cex_reverse_lane(
    strategy: CexDexStrategy,
    snapshot: dict,
    brain_conf: int,
) -> bool:
    try:
        reverse = strategy.reverse_strategy
        rev_ctx = snapshot.get("dex_cex_reverse") if isinstance(snapshot.get("dex_cex_reverse"), dict) else {}
        lane_score = float(rev_ctx.get("gross_bps") or rev_ctx.get("net_bps") or brain_conf)
        result = await reverse.scan_and_execute(brain_score=lane_score)
        status = str(result.get("status") or "")
        if result.get("live_fill"):
            return True
        if status in ("env_thresholds", "no_dex_cheap", "safety_blocked", "risk_blocked"):
            return False
        return status == "success"
    except Exception as exc:
        logger.warning("DEX-CEX reverse lane failed: %s", exc)
        return False


async def _run_liquidation_lane(strategy: CexDexStrategy) -> bool:
    try:
        from src.strategies.brain_signals import brain_snapshot
        from src.strategies.mev_dispatch import execute_mev_lane

        return await execute_mev_lane(
            "liquidation", brain_snapshot(), settings=strategy.settings
        )
    except Exception as exc:
        logger.warning("Liquidation lane failed: %s", exc)
        return False


async def _run_collateral_lane(strategy: CexDexStrategy) -> bool:
    try:
        from src.strategies.brain_signals import brain_snapshot
        from src.strategies.mev_dispatch import execute_mev_lane

        return await execute_mev_lane(
            "collateral_swap", brain_snapshot(), settings=strategy.settings
        )
    except Exception as exc:
        logger.warning("Collateral lane failed: %s", exc)
        return False
