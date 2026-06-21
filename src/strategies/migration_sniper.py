"""Pump.fun → Raydium migration sniper lane."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from src.strategies.meme_lanes_config import get_migration_sniper_settings
from src.strategies.meme_sniping.position import calculate_position_size, get_available_sol_balance
from src.strategies.meme_sniping.validator import validate_token
from src.strategies.position_manager import position_manager
from src.utils.ai import get_ai_decision

logger = logging.getLogger(__name__)

_PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"  # for future WS subscription
_SEEN_MIGRATIONS: dict[str, float] = {}
_DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"


@dataclass
class ValidationResult:
    safety_score: float
    passed: bool
    failed_checks: list[str]


async def advanced_token_validation(mint: str, coin: dict[str, Any]) -> ValidationResult:
    result = await validate_token(mint, coin)
    return ValidationResult(
        safety_score=float(result.get("safety_score") or 0),
        passed=bool(result.get("passed")),
        failed_checks=list(result.get("failed_checks") or []),
    )


async def get_migration_ai_score(event: dict[str, Any]) -> float:
    signal = {
        "token_address": event.get("mint"),
        "liquidity_usd": event.get("liquidity"),
        "volatility_bps": event.get("volatility_bps"),
        "migration_age_min": event.get("migration_age_min"),
        "dex_id": event.get("dex_id"),
        "evaluation_focus": "pump_fun_raydium_migration",
    }
    result = await get_ai_decision(signal, strategy="meme_sniping")
    return float(result.get("confidence") or 0)


def calculate_migration_size(ai_score: float, liquidity: float, sol_balance: float) -> float:
    cfg = get_migration_sniper_settings()
    base = calculate_position_size(ai_score, sol_balance, vol_bps=1200)
    liq_factor = min(1.0, float(liquidity or 0) / 30000.0)
    return min(cfg.max_trade_sol, base * max(0.5, liq_factor))


async def _fetch_recent_migrations(max_age_min: int) -> list[dict[str, Any]]:
    from src.strategies.meme_sniping.sources import fetch_candidate_coins

    coins, source = await fetch_candidate_coins(limit=25)
    now_ms = time.time() * 1000
    max_age_ms = max_age_min * 60 * 1000
    out: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=8.0) as client:
        for coin in coins:
            mint = str(coin.get("mint") or "")
            if not mint.lower().endswith("pump"):
                continue

            resp = await client.get(_DEX_TOKEN_URL.format(mint=mint))
            if resp.status_code != 200:
                continue
            pairs = resp.json().get("pairs") or []
            for pair in pairs:
                if str(pair.get("dexId") or "").lower() != "raydium":
                    continue
                created = int(pair.get("pairCreatedAt") or 0)
                if created <= 0 or (now_ms - created) > max_age_ms:
                    continue
                liq = float((pair.get("liquidity") or {}).get("usd") or coin.get("liquidity") or 0)
                pc = pair.get("priceChange") or {}
                vol_bps = int(abs(float(pc.get("m5") or 0)) * 100)
                age_min = (now_ms - created) / 60000.0
                out.append(
                    {
                        **coin,
                        "mint": mint,
                        "liquidity": liq,
                        "volatility_bps": vol_bps,
                        "dex_id": "raydium",
                        "pair_created_at": created,
                        "migration_age_min": round(age_min, 1),
                        "source": source,
                    }
                )
                break
    return out


async def is_migration_valid(event: dict[str, Any]) -> bool:
    cfg = get_migration_sniper_settings()
    liq = float(event.get("liquidity") or 0)
    age = float(event.get("migration_age_min") or 999)
    return liq >= 10000 and age <= cfg.max_age_minutes


async def execute_fast_buy(event: dict[str, Any], size_sol: float) -> None:
    cfg = get_migration_sniper_settings()
    mint = str(event.get("mint") or "")
    if not mint:
        return
    if cfg.simulate:
        logger.info(
            "[SIM] migration_sniper buy | mint=%s size_sol=%.3f liq=%.0f age_min=%.1f",
            mint[:12],
            size_sol,
            float(event.get("liquidity") or 0),
            float(event.get("migration_age_min") or 0),
        )
    await position_manager.open_via_execution(mint, size_sol, lane="migration_sniper")


async def migration_sniper_loop(shutdown_event=None) -> None:
    cfg = get_migration_sniper_settings()
    if not cfg.enabled:
        return

    logger.info(
        "Migration sniper started | simulate=%s min_safety=%.0f min_ai=%.0f max_age=%dm",
        cfg.simulate,
        cfg.min_safety_score,
        cfg.min_ai_score,
        cfg.max_age_minutes,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            return
        try:
            events = await _fetch_recent_migrations(cfg.max_age_minutes)
            for event in events:
                mint = str(event.get("mint") or "")
                if not mint:
                    continue
                last = _SEEN_MIGRATIONS.get(mint)
                if last and time.monotonic() - last < 1800:
                    continue

                if not await is_migration_valid(event):
                    continue

                validation = await advanced_token_validation(mint, event)
                if validation.safety_score < cfg.min_safety_score or not validation.passed:
                    logger.info(
                        "migration_sniper reject | mint=%s safety=%.1f failed=%s",
                        mint[:12],
                        validation.safety_score,
                        validation.failed_checks,
                    )
                    continue

                ai_score = await get_migration_ai_score(event)
                if ai_score < cfg.min_ai_score:
                    continue

                sol_balance = await get_available_sol_balance()
                size_sol = calculate_migration_size(
                    ai_score,
                    float(event.get("liquidity") or 0),
                    sol_balance,
                )
                if size_sol <= 0:
                    continue

                _SEEN_MIGRATIONS[mint] = time.monotonic()
                logger.info(
                    "migration_sniper signal | mint=%s ai=%.1f size_sol=%.3f safety=%.1f",
                    mint[:12],
                    ai_score,
                    size_sol,
                    validation.safety_score,
                )
                await execute_fast_buy(event, size_sol)

            await position_manager.monitor_positions()
            await asyncio.sleep(cfg.poll_interval_sec)
        except Exception as exc:
            logger.error("migration_sniper_loop error: %s", exc)
            await asyncio.sleep(3.0)
