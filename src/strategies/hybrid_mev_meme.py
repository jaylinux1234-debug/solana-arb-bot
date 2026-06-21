"""Hybrid MEV + meme snipe — primary snipe with optional backrun leg."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.strategies.meme_lanes_config import get_hybrid_mev_settings
from src.strategies.position_manager import position_manager

logger = logging.getLogger(__name__)


@dataclass
class MemeMevOpportunity:
    token_mint: str
    liquidity_usd: float
    m5_buy_usd: float
    has_large_buy_signal: bool
    size_sol: float
    lane_source: str = "hybrid_mev"


def _estimate_m5_buy_usd(coin: dict[str, Any]) -> float:
    try:
        vol_m5 = float((coin.get("volume") or {}).get("m5") or 0)
        if vol_m5 > 0:
            return vol_m5 * 0.55
        txns = coin.get("txns_m5_buys") or 0
        liq = float(coin.get("liquidity") or 0)
        return float(txns) * max(50.0, liq * 0.002)
    except (TypeError, ValueError):
        return 0.0


async def build_snipe_bundle(opportunity: MemeMevOpportunity) -> dict[str, Any]:
    """Simulated bundle descriptor (live path uses Jupiter via position_manager)."""
    return {
        "type": "snipe",
        "mint": opportunity.token_mint,
        "size_sol": opportunity.size_sol,
        "legs": 1,
    }


async def build_backrun_instruction(opportunity: MemeMevOpportunity) -> dict[str, Any] | None:
    if not opportunity.has_large_buy_signal:
        return None
    return {
        "type": "backrun",
        "mint": opportunity.token_mint,
        "note": "large_m5_buy_detected",
    }


def combine_bundles(snipe: dict[str, Any], backrun: dict[str, Any] | None) -> dict[str, Any]:
    legs = [snipe]
    if backrun:
        legs.append(backrun)
    return {"legs": legs, "leg_count": len(legs)}


async def submit_jito_bundle(bundle: dict[str, Any], *, tip_mult: float) -> dict[str, Any]:
    cfg = get_hybrid_mev_settings()
    if cfg.simulate:
        logger.info(
            "[SIM] hybrid_mev bundle | legs=%d tip_mult=%.2f bundle=%s",
            bundle.get("leg_count", 1),
            tip_mult,
            bundle,
        )
        return {"success": True, "simulated": True}
    # Live: primary snipe already routed through Jupiter in hybrid_meme_backrun
    return {"success": True, "simulated": False}


async def hybrid_meme_backrun(opportunity: MemeMevOpportunity) -> None:
    """Combine meme snipe with optional backrun for extra edge."""
    cfg = get_hybrid_mev_settings()
    snipe_bundle = await build_snipe_bundle(opportunity)

    backrun_ix = None
    if opportunity.has_large_buy_signal:
        backrun_ix = await build_backrun_instruction(opportunity)

    full_bundle = combine_bundles(snipe_bundle, backrun_ix)
    result = await submit_jito_bundle(full_bundle, tip_mult=cfg.jito_tip_mult)

    if result.get("success"):
        await position_manager.open_via_execution(
            opportunity.token_mint,
            opportunity.size_sol,
            lane="hybrid_mev_meme",
        )


async def evaluate_and_backrun(coin: dict[str, Any], *, lane_source: str) -> bool:
    """Called from other lanes when a token shows large buy pressure."""
    cfg = get_hybrid_mev_settings()
    if not cfg.enabled:
        return False

    m5_buy = _estimate_m5_buy_usd(coin)
    has_large = m5_buy >= cfg.min_m5_buy_usd
    if not has_large:
        return False

    mint = str(coin.get("mint") or "")
    if not mint:
        return False

    size_sol = min(cfg.max_trade_sol, 0.4 + m5_buy / 50000.0)
    opp = MemeMevOpportunity(
        token_mint=mint,
        liquidity_usd=float(coin.get("liquidity") or 0),
        m5_buy_usd=m5_buy,
        has_large_buy_signal=True,
        size_sol=size_sol,
        lane_source=lane_source,
    )
    logger.info(
        "hybrid_mev opportunity | mint=%s m5_buy_usd=%.0f size_sol=%.3f source=%s",
        mint[:12],
        m5_buy,
        size_sol,
        lane_source,
    )
    await hybrid_meme_backrun(opp)
    return True


async def hybrid_mev_watch_loop(shutdown_event=None) -> None:
    """Standalone watcher: re-scans high-volume tokens for hybrid plays."""
    import asyncio
    import time

    from src.strategies.meme_sniping.sources import fetch_candidate_coins

    cfg = get_hybrid_mev_settings()
    if not cfg.enabled:
        return

    logger.info(
        "Hybrid MEV meme lane started | simulate=%s min_m5_buy_usd=%.0f tip_mult=%.2f",
        cfg.simulate,
        cfg.min_m5_buy_usd,
        cfg.jito_tip_mult,
    )
    seen: dict[str, float] = {}

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            return
        try:
            coins, source = await fetch_candidate_coins(limit=15)
            for coin in coins:
                mint = str(coin.get("mint") or "")
                if not mint or (seen.get(mint) and time.monotonic() - seen[mint] < 900):
                    continue
                if await evaluate_and_backrun({**coin, "source": source}, lane_source="hybrid_watch"):
                    seen[mint] = time.monotonic()
            await position_manager.monitor_positions()
            await asyncio.sleep(3.0)
        except Exception as exc:
            logger.error("hybrid_mev_watch_loop error: %s", exc)
            await asyncio.sleep(4.0)
