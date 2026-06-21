"""Meme sniping execution v2 — Dex/Jupiter PnL monitor + optional Alchemy WS + live sell."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings
from src.strategies.meme_sniping.metrics import meme_sniping_metrics
from src.strategies.meme_sniping.position import next_tp_index, should_trailing_stop
from src.strategies.meme_sniping.sources import fetch_token_mark_price_usd

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


def _smooth_pnl_bps(position: dict[str, Any], pnl_bps: float) -> float:
    samples: list[float] = position.setdefault("pnl_samples", [])
    samples.append(float(pnl_bps))
    if len(samples) > 3:
        del samples[0]
    return float(statistics.median(samples))


def _position_age_sec(position: dict[str, Any]) -> float:
    return (datetime.now(UTC) - position["entry_time"]).total_seconds()


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


async def _estimate_pnl_bps_jupiter(token_mint: str, position: dict[str, Any]) -> float | None:
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


async def _estimate_pnl_bps_dex(token_mint: str, position: dict[str, Any]) -> float | None:
    """Mark-to-market via DexScreener USD price vs entry."""
    price = await fetch_token_mark_price_usd(token_mint)
    if price is None:
        return None
    entry_price = position.get("entry_price_usd")
    if entry_price is None:
        position["entry_price_usd"] = price
        return 0.0
    entry = float(entry_price)
    if entry <= 0:
        return None
    return ((price - entry) / entry) * 10_000.0


async def _estimate_pnl_bps(token_mint: str, position: dict[str, Any]) -> float | None:
    cfg = meme_sniping_settings
    dex_pnl = await _estimate_pnl_bps_dex(token_mint, position) if cfg.use_dex_price else None
    jup_pnl = await _estimate_pnl_bps_jupiter(token_mint, position)

    if dex_pnl is not None and jup_pnl is not None:
        # Prefer DexScreener; blend only when they roughly agree (reduces quote noise).
        if abs(dex_pnl - jup_pnl) <= 250:
            return (dex_pnl + jup_pnl) / 2.0
        return dex_pnl
    return dex_pnl if dex_pnl is not None else jup_pnl


async def execute_snipe(token_mint: str, size_sol: float) -> None:
    cfg = meme_sniping_settings
    if _check_daily_loss_cap():
        logger.warning("meme_snipe blocked | daily_loss_cap reached ($%.2f)", cfg.max_daily_loss_usd)
        return

    token_amount = await _entry_token_amount(token_mint, size_sol)
    entry_price_usd = await fetch_token_mark_price_usd(token_mint)

    if cfg.simulate:
        levels = cfg.tp_levels_bps
        logger.info(
            "[SIM] meme_snipe v3 | mint=%s size_sol=%.3f tp_levels=%s stop=%d "
            "grace=%ds trailing=%s token_amt=%s entry_usd=%s",
            token_mint,
            size_sol,
            levels,
            cfg.max_loss_bps,
            cfg.stop_grace_sec,
            cfg.enable_trailing_stop,
            token_amount or "n/a",
            f"{entry_price_usd:.8f}" if entry_price_usd else "n/a",
        )
        active_positions[token_mint] = _new_position(
            size_sol=size_sol,
            token_amount=token_amount or 0,
            simulated=True,
            entry_price_usd=entry_price_usd,
        )
        asyncio.create_task(monitor_position(token_mint))
        return

    try:
        from src.config.settings import get_settings
        from src.core.signer import HotWalletSigner
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
            logger.warning("meme_snipe quote failed | mint=%s", token_mint)
            return

        signer = HotWalletSigner.get_keypair()
        tip_est = int(
            float(getattr(settings, "JITO_TIP_LAMPORTS", 50_000) or 50_000) * cfg.jito_tip_mult
        )
        logger.info(
            "meme_snipe live buy | mint=%s size_sol=%.3f jito_tip_est=%d",
            token_mint[:12],
            size_sol,
            tip_est,
        )
        ok = await jup.swap(quote, signer)
        if not ok:
            logger.warning("meme_snipe swap failed | mint=%s", token_mint)
            return

        out_amount = int(quote.get("outAmount") or quote.get("out_amount") or token_amount or 0)
        active_positions[token_mint] = _new_position(
            size_sol=size_sol,
            token_amount=out_amount,
            simulated=False,
            entry_price_usd=entry_price_usd,
        )
        logger.info("meme_snipe executed v3 | mint=%s size_sol=%.3f", token_mint, size_sol)
        asyncio.create_task(monitor_position(token_mint))
    except Exception as exc:
        logger.error("meme_snipe execute failed | mint=%s err=%s", token_mint, exc)


def _new_position(
    *,
    size_sol: float,
    token_amount: int,
    simulated: bool,
    entry_price_usd: float | None,
) -> dict[str, Any]:
    return {
        "entry_time": datetime.now(UTC),
        "size_sol": size_sol,
        "token_amount": token_amount,
        "simulated": simulated,
        "entry_price_usd": entry_price_usd,
        "entry_price": None,
        "peak_price": None,
        "pnl_samples": [],
        "stop_breach_count": 0,
        "tp_hits": 0,
        "peak_pnl_bps": 0.0,
    }


async def process_pnl_bps_update(
    token_mint: str,
    raw_pnl_bps: float,
    position: dict[str, Any],
) -> bool:
    """Apply partial TP ladder, trailing stop, hard stop, and time exit."""
    cfg = meme_sniping_settings
    pnl_bps = _smooth_pnl_bps(position, raw_pnl_bps)
    age_sec = _position_age_sec(position)

    if should_trailing_stop(position, pnl_bps):
        await sell_position(token_mint, "trailing_stop", pnl_bps)
        return True

    levels = cfg.tp_levels_bps
    fractions = cfg.tp_partial_fractions
    tp_idx = next_tp_index(position)

    for i in range(tp_idx, len(levels)):
        level = levels[i]
        if pnl_bps < level:
            break
        frac = fractions[i] if i < len(fractions) else 1.0
        is_last = i == len(levels) - 1
        if is_last or frac >= 0.99:
            await sell_position(token_mint, f"TP{i + 1} (+{level}bps)", pnl_bps)
            return True
        await partial_sell(token_mint, frac, f"TP{i + 1} partial (+{level}bps)", pnl_bps)
        position["tp_hits"] = i + 1
        return False

    if pnl_bps <= cfg.max_loss_bps:
        if age_sec < cfg.stop_grace_sec:
            logger.debug(
                "meme_snipe stop suppressed (grace) | mint=%s pnl_bps=%.1f age=%.0fs",
                token_mint[:12],
                pnl_bps,
                age_sec,
            )
            return False
        position["stop_breach_count"] = int(position.get("stop_breach_count") or 0) + 1
        if position["stop_breach_count"] < cfg.stop_confirm_polls:
            logger.debug(
                "meme_snipe stop pending confirm | mint=%s pnl_bps=%.1f count=%d/%d",
                token_mint[:12],
                pnl_bps,
                position["stop_breach_count"],
                cfg.stop_confirm_polls,
            )
            return False
        await sell_position(token_mint, f"HARD STOP LOSS ({cfg.max_loss_bps}bps)", pnl_bps)
        return True

    position["stop_breach_count"] = 0

    if age_sec > cfg.max_hold_minutes * 60:
        await sell_position(token_mint, "Time Exit", pnl_bps)
        return True
    return False


async def process_price_update(token_mint: str, current_price: float, position: dict[str, Any]) -> bool:
    """Legacy price-ratio path (Alchemy WS)."""
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

    return await process_pnl_bps_update(token_mint, pnl_bps, position)


async def monitor_position(token_mint: str) -> None:
    """Dex/Jupiter polling; optional Alchemy WS overlay for live."""
    cfg = meme_sniping_settings
    position = active_positions.get(token_mint)
    if not position:
        return

    ws_url = _alchemy_ws_url() if cfg.use_alchemy else ""
    if ws_url and not position.get("simulated"):
        try:
            await asyncio.wait_for(
                _monitor_via_alchemy_ws(token_mint, ws_url, position),
                timeout=(cfg.max_hold_minutes + 2) * 60,
            )
            return
        except TimeoutError:
            logger.info("meme_snipe Alchemy WS timed out | mint=%s — using poll fallback", token_mint[:12])
        except Exception as exc:
            logger.error(
                "Alchemy WS monitor failed | mint=%s err=%s — using poll fallback",
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

    cfg = meme_sniping_settings
    deadline = position["entry_time"] + timedelta(minutes=cfg.max_hold_minutes + 2)
    last_ws_price_at = datetime.now(UTC)

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
                raw = await asyncio.wait_for(ws.recv(), timeout=min(25.0, cfg.poll_interval_sec + 2))
            except TimeoutError:
                if (datetime.now(UTC) - last_ws_price_at).total_seconds() > 30:
                    pnl_bps = await _estimate_pnl_bps(token_mint, position)
                    if pnl_bps is not None and await process_pnl_bps_update(token_mint, pnl_bps, position):
                        return
                continue

            data = json.loads(raw)
            if data.get("error"):
                logger.warning(
                    "meme_snipe Alchemy WS error | mint=%s err=%s",
                    token_mint[:12],
                    data.get("error"),
                )
                return

            result = data.get("result")
            if isinstance(result, dict):
                price = result.get("price")
                if price and float(price) > 0:
                    last_ws_price_at = datetime.now(UTC)
                    if await process_price_update(token_mint, float(price), position):
                        return

            await asyncio.sleep(0.4)


async def fallback_monitor(token_mint: str) -> None:
    """DexScreener + Jupiter polling (simulate and live fallback)."""
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
                if await process_pnl_bps_update(token_mint, pnl_bps, position):
                    return

            if _position_age_sec(position) > cfg.max_hold_minutes * 60:
                await sell_position(token_mint, "Time Exit", pnl_bps)
                return

            await asyncio.sleep(cfg.poll_interval_sec)
        except Exception:
            await asyncio.sleep(cfg.poll_interval_sec + 1)


async def _execute_live_sell(
    token_mint: str,
    position: dict[str, Any],
    *,
    token_amount: int | None = None,
) -> bool:
    amount = int(token_amount or position.get("token_amount") or 0)
    if amount <= 0:
        logger.warning("meme_snipe sell skipped | mint=%s no token_amount", token_mint[:12])
        return False

    try:
        from src.config.settings import get_settings
        from src.core.signer import HotWalletSigner
        from src.dex.jupiter import JupiterClient

        settings = get_settings()
        jup = JupiterClient(settings)
        quote = await jup.get_quote_for_mints(
            input_mint=token_mint,
            output_mint=str(jup.SOL),
            amount=amount,
            slippage_bps=120,
        )
        if not quote:
            logger.warning("meme_snipe sell quote failed | mint=%s", token_mint[:12])
            return False

        signer = HotWalletSigner.get_keypair()
        ok = await jup.swap(quote, signer)
        if ok:
            logger.info("meme_snipe sell executed | mint=%s amount=%d", token_mint[:12], amount)
        return ok
    except Exception as exc:
        logger.error("meme_snipe sell failed | mint=%s err=%s", token_mint[:12], exc)
        return False


async def partial_sell(
    token_mint: str,
    fraction: float,
    reason: str,
    pnl_bps: float | None,
) -> None:
    """Sell a fraction of the position; keep monitoring the remainder."""
    cfg = meme_sniping_settings
    position = active_positions.get(token_mint)
    if not position:
        return

    frac = max(0.05, min(1.0, float(fraction)))
    token_amount = int(position.get("token_amount") or 0)
    sell_amt = max(1, int(token_amount * frac))
    if sell_amt >= token_amount:
        await sell_position(token_mint, reason, pnl_bps)
        return

    position["token_amount"] = token_amount - sell_amt
    position["size_sol"] = float(position.get("size_sol") or 0) * (1.0 - frac)
    meme_sniping_metrics.record_partial_exit(reason, pnl_bps, fraction=frac)

    if cfg.simulate or position.get("simulated"):
        logger.info(
            "[SIM] meme_snipe partial sell | mint=%s frac=%.0f%% reason=%s pnl_bps=%s remaining_amt=%d",
            token_mint,
            frac * 100,
            reason,
            f"{pnl_bps:.1f}" if pnl_bps is not None else "n/a",
            position["token_amount"],
        )
        return

    await _execute_live_sell(token_mint, position, token_amount=sell_amt)


async def sell_position(token_mint: str, reason: str, pnl_bps: float | None = None) -> None:
    global _daily_loss_usd
    cfg = meme_sniping_settings
    position = active_positions.pop(token_mint, None)
    sim = bool(position and position.get("simulated"))
    meme_sniping_metrics.record_exit(reason, pnl_bps=pnl_bps, mint=token_mint)

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

    logger.info("meme_snipe sell | mint=%s reason=%s pnl_bps=%s", token_mint, reason, pnl_bps)
    if position:
        await _execute_live_sell(token_mint, position)
