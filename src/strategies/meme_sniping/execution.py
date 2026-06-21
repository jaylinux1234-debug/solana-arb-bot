"""Meme sniping execution (simulate-first)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings

logger = logging.getLogger(__name__)

active_positions: dict[str, dict[str, Any]] = {}


async def execute_snipe(token_mint: str, size_sol: float) -> None:
    cfg = meme_sniping_settings
    if cfg.simulate:
        logger.info(
            "[SIM] meme_snipe | mint=%s size_sol=%.3f tp1=%dbps tp2=%dbps tp3=%dbps",
            token_mint,
            size_sol,
            cfg.profit_target_1_bps,
            cfg.profit_target_2_bps,
            cfg.profit_target_3_bps,
        )
        active_positions[token_mint] = {
            "entry_time": datetime.now(UTC),
            "size_sol": size_sol,
            "simulated": True,
        }
        asyncio.create_task(monitor_position(token_mint))
        return

    try:
        from src.config.settings import get_settings
        from src.dex.jupiter import JupiterClient
        from src.execution.jito import send_jito_bundle

        settings = get_settings()
        jup = JupiterClient(settings)
        lamports = int(size_sol * 1_000_000_000)
        quote = await jup.get_quote(
            input_mint=str(jup.SOL),
            output_mint=token_mint,
            amount=lamports,
            slippage_bps=90,
        )
        if not quote:
            logger.warning("meme_snipe quote failed | mint=%s", token_mint)
            return

        from src.core.signer import HotWalletSigner

        signer = HotWalletSigner.get_keypair()
        ok = await jup.swap(quote, signer)
        if not ok:
            logger.warning("meme_snipe swap failed | mint=%s", token_mint)
            return

        await send_jito_bundle([])
        active_positions[token_mint] = {
            "entry_time": datetime.now(UTC),
            "size_sol": size_sol,
            "simulated": False,
        }
        logger.info("meme_snipe executed | mint=%s size_sol=%.3f", token_mint, size_sol)
        asyncio.create_task(monitor_position(token_mint))
    except Exception as exc:
        logger.error("meme_snipe execute failed | mint=%s err=%s", token_mint, exc)


async def monitor_position(token_mint: str) -> None:
    cfg = meme_sniping_settings
    position = active_positions.get(token_mint)
    if not position:
        return

    deadline = position["entry_time"] + timedelta(minutes=cfg.max_hold_minutes)
    while datetime.now(UTC) < deadline:
        try:
            await asyncio.sleep(4)
            # Price feed + TP/SL hooks go here when live exits are wired.
        except Exception:
            await asyncio.sleep(5)

    logger.info(
        "meme_snipe position closed (max_hold) | mint=%s simulated=%s",
        token_mint,
        position.get("simulated"),
    )
    active_positions.pop(token_mint, None)
