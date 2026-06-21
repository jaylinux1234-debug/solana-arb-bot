"""Dynamic position sizing and trailing-stop helpers."""

from __future__ import annotations

from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings


async def get_available_sol_balance() -> float:
    try:
        from src.utils.inventory import get_sol_balance_async

        return max(0.0, float(await get_sol_balance_async()))
    except Exception:
        return 0.0


def calculate_position_size(
    signal_strength: float,
    sol_balance: float,
    *,
    vol_bps: int = 0,
) -> float:
    """Dynamic size from confidence, wallet balance, and volatility."""
    cfg = meme_sniping_settings
    base = min(cfg.max_trade_sol, sol_balance * 0.25)
    if base <= 0:
        base = min(cfg.max_trade_sol, 0.5)

    if signal_strength > 90:
        size_mult = 1.0
    elif signal_strength > 80:
        size_mult = 0.75
    else:
        size_mult = 0.45

    vol = max(1, vol_bps)
    vol_factor = min(1.0, 1200.0 / vol)
    size = base * size_mult * vol_factor
    return max(0.25, min(cfg.max_trade_sol, size))


def should_trailing_stop(position: dict[str, Any], pnl_bps: float) -> bool:
    cfg = meme_sniping_settings
    if not cfg.enable_trailing_stop:
        return False
    peak = float(position.get("peak_pnl_bps") or pnl_bps)
    if pnl_bps > peak:
        position["peak_pnl_bps"] = pnl_bps
        peak = pnl_bps
    if peak < cfg.trailing_arm_bps:
        return False
    return pnl_bps < (peak - cfg.trailing_stop_bps)


def next_tp_index(position: dict[str, Any]) -> int:
    return int(position.get("tp_hits") or 0)
