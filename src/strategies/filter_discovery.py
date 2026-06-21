"""Filter-based systematic token discovery lane."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src.strategies.hybrid_mev_meme import evaluate_and_backrun
from src.strategies.meme_lanes_config import get_filter_discovery_settings
from src.strategies.meme_sniping.sources import fetch_candidate_coins
from src.strategies.meme_sniping.validator import check_dev_wallet_percentage, is_lp_burned
from src.strategies.position_manager import position_manager

logger = logging.getLogger(__name__)

_seen_mints: dict[str, float] = {}


class TokenFilterProfile:
    def __init__(self) -> None:
        cfg = get_filter_discovery_settings()
        self.min_liq = cfg.min_liq_usd
        self.min_vol_5m_bps = cfg.min_vol_5m_bps
        self.max_dev_pct = cfg.max_dev_pct
        self.min_social = cfg.min_social
        self.require_burned_lp = cfg.require_burned_lp

    async def score_token(self, token_data: dict[str, Any]) -> float:
        score = 0.0
        liq = float(token_data.get("liquidity") or token_data.get("liq_usd") or 0)
        vol = int(token_data.get("volatility_bps") or token_data.get("vol_5m_bps") or 0)
        social = int(token_data.get("social_score") or 0)
        mint = str(token_data.get("mint") or "")

        dev_pct = float(token_data.get("dev_percentage") or 0)
        if dev_pct <= 0 and mint:
            dev_pct = await check_dev_wallet_percentage(mint, token_data)

        lp_burned = token_data.get("lp_burned")
        if lp_burned is None and mint:
            lp_burned = await is_lp_burned(mint, token_data)

        if liq >= self.min_liq:
            score += 30
        if vol >= self.min_vol_5m_bps:
            score += 25
        if dev_pct <= self.max_dev_pct:
            score += 20
        if social >= self.min_social:
            score += 15
        if lp_burned or not self.require_burned_lp:
            score += 10

        token_data["filter_score"] = score
        token_data["dev_pct"] = dev_pct
        token_data["lp_burned"] = bool(lp_burned)
        return score


async def trigger_snipe(token_data: dict[str, Any], score: float) -> None:
    cfg = get_filter_discovery_settings()
    mint = str(token_data.get("mint") or "")
    if not mint:
        return

    size_sol = min(cfg.max_trade_sol, 0.35 + (score - cfg.min_score) * 0.02)
    if cfg.simulate:
        logger.info(
            "[SIM] filter_discovery snipe | mint=%s score=%.1f size_sol=%.3f liq=%.0f vol=%d",
            mint[:12],
            score,
            size_sol,
            float(token_data.get("liquidity") or 0),
            int(token_data.get("volatility_bps") or 0),
        )
    await position_manager.open_via_execution(mint, size_sol, lane="filter_discovery")


async def discovery_loop(shutdown_event=None) -> None:
    cfg = get_filter_discovery_settings()
    if not cfg.enabled:
        return

    profile = TokenFilterProfile()
    logger.info(
        "Filter discovery lane started | simulate=%s min_score=%.0f min_liq=%.0f",
        cfg.simulate,
        cfg.min_score,
        cfg.min_liq_usd,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            return
        try:
            coins, source = await fetch_candidate_coins(limit=20)
            for coin in coins:
                mint = str(coin.get("mint") or "")
                if not mint:
                    continue
                last = _seen_mints.get(mint)
                if last and time.monotonic() - last < 1200:
                    continue

                token = {**coin, "source": source}
                score = await profile.score_token(token)
                if score >= cfg.min_score:
                    _seen_mints[mint] = time.monotonic()
                    logger.info(
                        "filter_discovery hit | mint=%s score=%.1f source=%s",
                        mint[:12],
                        score,
                        source,
                    )
                    await trigger_snipe(token, score)
                    await evaluate_and_backrun(token, lane_source="filter_discovery")

            await position_manager.monitor_positions()
            await asyncio.sleep(cfg.poll_interval_sec)
        except Exception as exc:
            logger.error("filter_discovery_loop error: %s", exc)
            await asyncio.sleep(2.0)
