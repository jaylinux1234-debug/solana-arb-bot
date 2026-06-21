"""Meme sniping execution v2 — TP ladder + hard stop (simulate-first)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings

logger = logging.getLogger(__name__)

active_positions: dict[str, dict[str, Any]] = {}


async def _estimate_pnl_bps(token_mint: str, entry_sol: float) -> float | None:
    """Rough mark-to-market via Jupiter sell quote (SOL out vs entry)."""
    if entry_sol <= 0:
        return None
    try:
        from src.config.settings import get_settings
        from src.dex.jupiter import JupiterClient

        settings = get_settings()
        jup = JupiterClient(settings)
        # Placeholder token amount — live path should store actual token qty at entry.
        token_amount = max(1, int(entry_sol * 1_000_000_000))
        quote = await jup.get_quote(
            input_mint=token_mint,
            output_mint=str(jup.SOL),
            amount=token_amount,
            slippage_bps=85,
        )
        if not quote:
            return None
        out_lamports = int(quote.get("outAmount") or quote.get("out_amount") or 0)
        if out_lamports <= 0:
            return None
        out_sol = out_lamports / 1_000_000_000.0
        return ((out_sol - entry_sol) / entry_sol) * 10_000.0
    except Exception as exc:
        logger.debug("meme_snipe pnl estimate failed | mint=%s err=%s", token_mint[:12], exc)
        return None


async def execute_snipe(token_mint: str, size_sol: float) -> None:
    cfg = meme_sniping_settings
    if cfg.simulate:
        logger.info(
            "[SIM] meme_snipe v2 | mint=%s size_sol=%.3f tp1=%d tp2=%d tp3=%d stop=%d",
            token_mint,
            size_sol,
            cfg.profit_target_1_bps,
            cfg.profit_target_2_bps,
            cfg.profit_target_3_bps,
            cfg.max_loss_bps,
        )
        active_positions[token_mint] = {
            "entry_time": datetime.now(UTC),
            "size_sol": size_sol,
            "simulated": True,
            "peak_price": None,
        }
        asyncio.create_task(monitor_position(token_mint))
        return

    try:
        from src.config.settings import get_settings
        from src.core.signer import HotWalletSigner
        from src.dex.jupiter import JupiterClient
        from src.execution.jito import send_jito_bundle

        settings = get_settings()
        jup = JupiterClient(settings)
        lamports = int(size_sol * 1_000_000_000)
        quote = await jup.get_quote(
            input_mint=str(jup.SOL),
            output_mint=token_mint,
            amount=lamports,
            slippage_bps=85,
        )
        if not quote:
            logger.warning("meme_snipe quote failed | mint=%s", token_mint)
            return

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
            "peak_price": None,
        }
        logger.info("meme_snipe executed v2 | mint=%s size_sol=%.3f", token_mint, size_sol)
        asyncio.create_task(monitor_position(token_mint))
    except Exception as exc:
        logger.error("meme_snipe execute failed | mint=%s err=%s", token_mint, exc)


async def monitor_position(token_mint: str) -> None:
    """Fast TP ladder + hard stop + time exit."""
    cfg = meme_sniping_settings
    position = active_positions.get(token_mint)
    if not position:
        return

    while True:
        try:
            pnl_bps = await _estimate_pnl_bps(
                token_mint, float(position.get("size_sol") or 0)
            )
            if pnl_bps is not None:
                if pnl_bps >= cfg.profit_target_3_bps:
                    await sell_position(token_mint, f"TP3 +{cfg.profit_target_3_bps}bps")
                    break
                if pnl_bps >= cfg.profit_target_2_bps:
                    await sell_position(token_mint, f"TP2 +{cfg.profit_target_2_bps}bps")
                    break
                if pnl_bps >= cfg.profit_target_1_bps:
                    await sell_position(token_mint, f"TP1 +{cfg.profit_target_1_bps}bps")
                    break
                if pnl_bps <= cfg.max_loss_bps:
                    await sell_position(
                        token_mint,
                        f"HARD STOP LOSS ({cfg.max_loss_bps}bps)",
                    )
                    break

            age = datetime.now(UTC) - position["entry_time"]
            if age > timedelta(minutes=cfg.max_hold_minutes):
                await sell_position(token_mint, "Time Exit")
                break

            await asyncio.sleep(3)
        except Exception:
            await asyncio.sleep(5)


async def sell_position(token_mint: str, reason: str) -> None:
    cfg = meme_sniping_settings
    position = active_positions.pop(token_mint, None)
    sim = bool(position and position.get("simulated"))
    if cfg.simulate or sim:
        logger.info("[SIM] meme_snipe sell | mint=%s reason=%s", token_mint, reason)
        return
    logger.info("meme_snipe sell | mint=%s reason=%s", token_mint, reason)
