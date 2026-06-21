"""Meme sniping execution v2 — Jupiter PnL monitor + optional Alchemy WS."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings
from src.strategies.meme_sniping.metrics import meme_sniping_metrics

logger = logging.getLogger(__name__)

active_positions: dict[str, dict[str, Any]] = {}
_daily_loss_usd: float = 0.0
_daily_loss_day: str = ""


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


def _check_daily_loss_cap() -> bool:
    """Return True when daily loss budget is exhausted."""
    global _daily_loss_day, _daily_loss_usd
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if _daily_loss_day != today:
        _daily_loss_day = today
        _daily_loss_usd = 0.0
    return _daily_loss_usd >= meme_sniping_settings.max_daily_loss_usd


async def _entry_token_amount(token_mint: str, size_sol: float) -> int | None:
    """Estimate token out-amount for a SOL buy (used for PnL polling)."""
    try:
        from src.config.settings import get_settings
        from src.dex.jupiter import JupiterClient

        settings = get_settings()
        jup = JupiterClient(settings)
        lamports = int(size_sol * 1_000_000_000)
        quote = await jup.get_quote_for_mints(
            input_mint=str(jup.SOL),
            output_mint=token_mint,
            amount=lamports,
            slippage_bps=85,
        )
        if not quote:
            return None
        out_amount = int(quote.get("outAmount") or quote.get("out_amount") or 0)
        return out_amount if out_amount > 0 else None
    except Exception as exc:
        logger.debug("meme_snipe entry quote failed | mint=%s err=%s", token_mint[:12], exc)
        return None


async def _estimate_pnl_bps(token_mint: str, position: dict[str, Any]) -> float | None:
    """Mark-to-market via Jupiter sell quote."""
    entry_sol = float(position.get("size_sol") or 0)
    token_amount = int(position.get("token_amount") or 0)
    if entry_sol <= 0 or token_amount <= 0:
        return None
    try:
        from src.config.settings import get_settings
        from src.dex.jupiter import JupiterClient

        settings = get_settings()
        jup = JupiterClient(settings)
        quote = await jup.get_quote_for_mints(
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
    if _check_daily_loss_cap():
        logger.warning("meme_snipe blocked | daily_loss_cap reached ($%.2f)", cfg.max_daily_loss_usd)
        return

    token_amount = await _entry_token_amount(token_mint, size_sol)

    if cfg.simulate:
        logger.info(
            "[SIM] meme_snipe v2 | mint=%s size_sol=%.3f tp1=%d tp2=%d tp3=%d stop=%d token_amt=%s",
            token_mint,
            size_sol,
            cfg.profit_target_1_bps,
            cfg.profit_target_2_bps,
            cfg.profit_target_3_bps,
            cfg.max_loss_bps,
            token_amount or "n/a",
        )
        active_positions[token_mint] = {
            "entry_time": datetime.now(UTC),
            "size_sol": size_sol,
            "token_amount": token_amount or 0,
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
        quote = await jup.get_quote_for_mints(
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

        out_amount = int(quote.get("outAmount") or quote.get("out_amount") or token_amount or 0)
        await send_jito_bundle([])
        active_positions[token_mint] = {
            "entry_time": datetime.now(UTC),
            "size_sol": size_sol,
            "token_amount": out_amount,
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
        await sell_position(token_mint, f"TP3 (+{cfg.profit_target_3_bps}bps)", pnl_bps)
        return True
    if pnl_bps >= cfg.profit_target_2_bps:
        await sell_position(token_mint, f"TP2 (+{cfg.profit_target_2_bps}bps)", pnl_bps)
        return True
    if pnl_bps >= cfg.profit_target_1_bps:
        await sell_position(token_mint, f"TP1 (+{cfg.profit_target_1_bps}bps)", pnl_bps)
        return True
    if pnl_bps <= cfg.max_loss_bps:
        await sell_position(token_mint, f"HARD STOP LOSS ({cfg.max_loss_bps}bps)", pnl_bps)
        return True

    age = datetime.now(UTC) - position["entry_time"]
    if age > timedelta(minutes=cfg.max_hold_minutes):
        await sell_position(token_mint, "Time Exit", pnl_bps)
        return True
    return False


async def monitor_position(token_mint: str) -> None:
    """Jupiter quote polling (simulate + fallback); optional Alchemy WS for live."""
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

    if not position.get("token_amount"):
        refreshed = await _entry_token_amount(token_mint, float(position.get("size_sol") or 0))
        if refreshed:
            position["token_amount"] = refreshed

    while token_mint in active_positions:
        try:
            pnl_bps = await _estimate_pnl_bps(token_mint, position)
            if pnl_bps is not None:
                entry_price = float(position.get("entry_price") or 1.0)
                synthetic_price = entry_price * (1.0 + pnl_bps / 10_000.0)
                if position.get("entry_price") is None:
                    position["entry_price"] = 1.0
                    position["peak_price"] = 1.0
                if await process_price_update(token_mint, synthetic_price, position):
                    return

            age = datetime.now(UTC) - position["entry_time"]
            if age > timedelta(minutes=cfg.max_hold_minutes):
                await sell_position(token_mint, "Time Exit", pnl_bps)
                return

            await asyncio.sleep(3)
        except Exception:
            await asyncio.sleep(5)


async def sell_position(token_mint: str, reason: str, pnl_bps: float | None = None) -> None:
    global _daily_loss_usd
    cfg = meme_sniping_settings
    position = active_positions.pop(token_mint, None)
    sim = bool(position and position.get("simulated"))
    meme_sniping_metrics.record_exit(reason)

    if pnl_bps is not None and pnl_bps < 0 and position:
        size_sol = float(position.get("size_sol") or 0)
        sol_usd = float(os.getenv("SOL_USD_ESTIMATE", "150") or 150)
        loss_usd = abs(pnl_bps / 10_000.0 * size_sol * sol_usd)
        _daily_loss_usd += loss_usd

    if cfg.simulate or sim:
        logger.info(
            "[SIM] meme_snipe sell | mint=%s reason=%s pnl_bps=%s",
            token_mint,
            reason,
            f"{pnl_bps:.1f}" if pnl_bps is not None else "n/a",
        )
        return
    logger.info("meme_snipe sell | mint=%s reason=%s", token_mint, reason)
    # TODO: Jupiter sell leg when MEME_SNIPING_SIMULATE=false
