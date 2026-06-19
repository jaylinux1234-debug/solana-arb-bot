"""Dynamic per-pair sizing from .env with fallback."""

from __future__ import annotations

import os
from typing import Dict

_DEFAULT_MAX_MICRO = 35_000_000

_PAIR_MAX_MICRO: Dict[str, int] = {
    "BONK": 42_000_000,
    "WIF": 42_000_000,
    "POPCAT": 32_000_000,
    "MEW": 28_000_000,
    "JUP": 35_000_000,
    "DRIFT": 30_000_000,
    "BRETT": 25_000_000,
    "MOODENG": 22_000_000,
    "GIGA": 25_000_000,
    "PNUT": 20_000_000,
    "FARTCOIN": 18_000_000,
    "DOG": 22_000_000,
    "TURBO": 20_000_000,
    "MICH": 18_000_000,
    "SOL": 35_000_000,
}


def get_max_trade_size_micro(pair_symbol: str) -> int:
    """Read ``PAIR_MAX_SIZE_{SYMBOL}`` from env (USDC), fallback to dict."""
    symbol = pair_symbol.upper()
    env_key = f"PAIR_MAX_SIZE_{symbol}"
    env_value = (os.getenv(env_key) or "").strip()
    if env_value:
        try:
            return max(1, int(float(env_value) * 1_000_000))
        except ValueError:
            pass
    return _PAIR_MAX_MICRO.get(symbol, _DEFAULT_MAX_MICRO)


def get_max_trade_size_usdc(pair_symbol: str) -> float:
    return get_max_trade_size_micro(pair_symbol) / 1_000_000


def calculate_trade_size(
    pair_symbol: str,
    gross_bps: float,
    global_max_usdc: float = 42.0,
) -> int:
    """Scale size based on gross strength for better net modeling."""
    max_micro = get_max_trade_size_micro(pair_symbol)
    global_max_micro = int(global_max_usdc * 1_000_000)
    base_size = min(max_micro, global_max_micro)

    if gross_bps < 10:
        scale = 0.65
    elif gross_bps < 18:
        scale = 0.85
    else:
        scale = 1.0

    return int(base_size * scale)


def pair_max_flash_usdc(symbol: str | None, default: float | None = None) -> float:
    sym = (symbol or "").strip().upper()
    if sym:
        return get_max_trade_size_usdc(sym)
    if default is not None:
        return float(default)
    return get_max_trade_size_usdc("SOL")


def dynamic_flash_size(
    base: int = 150_000,
    utilization: float = 0.68,
    volatility: float = 80,
) -> int:
    volatility_factor = max(0.6, 1.0 - (volatility / 300))
    size = int(base * utilization * volatility_factor)
    return max(30_000, min(500_000, size))
