"""Meme sniping execution v2 — Alchemy WS monitor + Jupiter fallback (simulate-first)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings

logger = logging.getLogger(__name__)

active_positions: dict[str, dict[str, Any]] = {}


def _alchemy_ws_url() -> str:
    """Resolve Alchemy WSS from env — never hardcode API keys in source."""
    for name in ("MEME_SNIPING_ALCHEMY_WS_URL", "SOLANA_RPC_WS_URL", "SOLANA_RPC_WS_FAST"):
        url = (os.getenv(name) or "").strip()
        if url and "alchemy.com" in url.lower():
            return url
    key = (os.getenv("ALCHEMY_KEY") or "").strip()
    if key:
        return f"wss://solana-mainnet.g.alchemy.com/v2/{key}"
    http = (os.getenv("SOLANA_RPC_URL") or os.getenv("ALCHEMY_RPC") or "").strip()
    if "alchemy.com" in http.lower() and http.startswith("https://"):
        return "wss://" + http[len("https://") :]
    return ""


async def _estimate_pnl_bps(token_mint: str, entry_sol: float) -> float | None:
    """Mark-to-market via Jupiter sell quote when WS price feed is unavailable."""
    if entry_sol <= 0:
        return None
    try:
        from src.config.settings import get_settings
        from src.dex.jupiter import JupiterClient

        settings = get_settings()
        jup = JupiterClient(settings)
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
            "entry_price": None,
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
            "entry_price": None,
            "peak_price": None,
        }
        logger.info("meme_snipe executed v2 | mint=%s size_sol=%.3f", token_mint, size_sol)
        asyncio.create_task(monitor_position(token_mint))
    except Exception as exc:
        logger.error("meme_snipe execute failed | mint=%s err=%s", token_mint, exc)


async def process_price_update(token_mint: str, current_price: float, position: dict[str, Any]) -> bool:
    """Apply TP ladder / stop / time exit. Returns True when position closed."""
    cfg = meme_sniping_settings

    if position.get("entry_price") is None:
        position["entry_price"] = current_price
        position["peak_price"] = current_price

    entry = float(position["entry_price"] or current_price)
    if entry <= 0:
        return False

    pnl_bps = (current_price - entry) / entry * 10_000.0
    peak = float(position.get("peak_price") or current_price)
    if current_price > peak:
        position["peak_price"] = current_price

    if pnl_bps >= cfg.profit_target_3_bps:
        await sell_position(token_mint, f"TP3 (+{cfg.profit_target_3_bps}bps)")
        return True
    if pnl_bps >= cfg.profit_target_2_bps:
        await sell_position(token_mint, f"TP2 (+{cfg.profit_target_2_bps}bps)")
        return True
    if pnl_bps >= cfg.profit_target_1_bps:
        await sell_position(token_mint, f"TP1 (+{cfg.profit_target_1_bps}bps)")
        return True
    if pnl_bps <= cfg.max_loss_bps:
        await sell_position(token_mint, f"HARD STOP LOSS ({cfg.max_loss_bps}bps)")
        return True

    age = datetime.now(UTC) - position["entry_time"]
    if age > timedelta(minutes=cfg.max_hold_minutes):
        await sell_position(token_mint, "Time Exit")
        return True
    return False


async def monitor_position(token_mint: str) -> None:
    """Prefer Alchemy WS price feed; fall back to Jupiter quote polling."""
    cfg = meme_sniping_settings
    position = active_positions.get(token_mint)
    if not position:
        return

    if cfg.use_alchemy and not position.get("simulated"):
        ws_url = _alchemy_ws_url()
        if ws_url:
            try:
                await _monitor_via_alchemy_ws(token_mint, ws_url, position)
                return
            except Exception as exc:
                logger.error(
                    "Alchemy WS monitor failed | mint=%s err=%s — using Jupiter fallback",
                    token_mint[:12],
                    exc,
                )

    await fallback_monitor(token_mint)


async def _monitor_via_alchemy_ws(
    token_mint: str,
    ws_url: str,
    position: dict[str, Any],
) -> None:
    import websockets

    deadline = position["entry_time"] + timedelta(minutes=meme_sniping_settings.max_hold_minutes + 2)
    async with websockets.connect(ws_url, ping_interval=20, open_timeout=15) as ws:
        sub_msg = {
            "jsonrpc": "2.0",
            "method": "subscribe",
            "params": ["tokenPrice", {"token": token_mint}],
            "id": 1,
        }
        await ws.send(json.dumps(sub_msg))
        logger.info("meme_snipe Alchemy WS subscribed | mint=%s", token_mint[:12])

        while datetime.now(UTC) < deadline and token_mint in active_positions:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=25.0)
            except TimeoutError:
                if await process_price_update(token_mint, float(position.get("entry_price") or 0), position):
                    return
                continue

            data = json.loads(raw)
            result = data.get("result")
            if isinstance(result, dict):
                price = result.get("price")
                if price and float(price) > 0:
                    if await process_price_update(token_mint, float(price), position):
                        return

            await asyncio.sleep(0.4)


async def fallback_monitor(token_mint: str) -> None:
    """Jupiter quote polling when Alchemy WS is unavailable or in simulate mode."""
    cfg = meme_sniping_settings
    position = active_positions.get(token_mint)
    if not position:
        return

    while token_mint in active_positions:
        try:
            pnl_bps = await _estimate_pnl_bps(token_mint, float(position.get("size_sol") or 0))
            if pnl_bps is not None:
                entry_price = float(position.get("entry_price") or 1.0)
                synthetic_price = entry_price * (1.0 + pnl_bps / 10_000.0)
                if await process_price_update(token_mint, synthetic_price, position):
                    return

            age = datetime.now(UTC) - position["entry_time"]
            if age > timedelta(minutes=cfg.max_hold_minutes):
                await sell_position(token_mint, "Time Exit")
                return

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
    # TODO: Jupiter sell leg when MEME_SNIPING_SIMULATE=false
