"""CEX-DEX tradable pairs from ``CEX_MIDCAPS`` + SOL (Backpack ``{SYM}_USDC``)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from src.core.tokens import TOKEN_DECIMALS, get_token_mint

SOL_SYMBOL = "SOL"


@dataclass(frozen=True)
class CexDexPair:
    symbol: str
    backpack_symbol: str
    pair_label: str
    base_mint: str
    base_decimals: int


def _parse_midcap_symbols() -> list[str]:
    raw = (os.getenv("CEX_MIDCAPS") or "").strip()
    if not raw:
        try:
            from src.config.settings import get_settings

            raw = (get_settings().CEX_MIDCAPS or "").strip()
        except Exception:
            raw = ""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        sym = part.strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    max_n = max(1, int(os.getenv("CEX_MAX_MIDCAPS", "20")))
    return out[:max_n]


def _pair_from_symbol(symbol: str) -> CexDexPair | None:
    sym = symbol.strip().upper()
    mint = get_token_mint(sym)
    if not mint:
        return None
    dec = int(TOKEN_DECIMALS.get(sym, 6))
    return CexDexPair(
        symbol=sym,
        backpack_symbol=f"{sym}_USDC",
        pair_label=f"{sym}/USDC",
        base_mint=mint,
        base_decimals=dec,
    )


def load_cex_dex_pairs(*, include_sol: bool = True) -> list[CexDexPair]:
    """
    Ordered pair list: SOL first (if enabled), then ``CEX_MIDCAPS`` up to ``CEX_MAX_MIDCAPS``.
    """
    pairs: list[CexDexPair] = []
    if include_sol and os.getenv("CEX_DEX_INCLUDE_SOL", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        sol = _pair_from_symbol(SOL_SYMBOL)
        if sol:
            pairs.append(sol)

    for sym in _parse_midcap_symbols():
        if sym == SOL_SYMBOL:
            continue
        p = _pair_from_symbol(sym)
        if p is None:
            continue
        if any(x.symbol == p.symbol for x in pairs):
            continue
        pairs.append(p)
    return pairs


def pair_by_symbol(symbol: str) -> CexDexPair | None:
    sym = symbol.strip().upper()
    for p in load_cex_dex_pairs():
        if p.symbol == sym:
            return p
    return None
