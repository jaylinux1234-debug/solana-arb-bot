"""Robust Backpack ticker / depth JSON parsing (handles empty or variant payloads)."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_PRICE_KEYS = (
    "lastPrice",
    "last",
    "close",
    "markPrice",
    "indexPrice",
    "price",
)


def parse_json_body(text: str) -> Any | None:
    """Parse HTTP body; return None on empty or invalid JSON (no exception)."""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def normalize_ticker_row(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    return row


def ticker_rows_from_payload(data: Any, symbol: str | None = None) -> list[dict[str, Any]]:
    """Flatten Backpack ticker response into zero or more ticker dicts."""
    if data is None:
        return []

    sym = (symbol or "").strip().upper().replace("/", "_")

    if isinstance(data, list):
        rows = [normalize_ticker_row(x) for x in data]
        rows = [r for r in rows if r]
        if not sym:
            return rows
        return [
            r
            for r in rows
            if str(r.get("symbol", r.get("s", ""))).upper().replace("/", "_") == sym
        ]

    if not isinstance(data, dict):
        return []

    if sym:
        for key in ("symbol", "s", "market"):
            if str(data.get(key, "")).upper().replace("/", "_") == sym:
                return [data]

    nested = data.get("data") or data.get("result") or data.get("tickers")
    if nested is not None and nested is not data:
        return ticker_rows_from_payload(nested, symbol=sym or None)

    if any(k in data for k in _PRICE_KEYS):
        return [data]

    return []


def mid_price_from_ticker(data: Any, symbol: str | None = None) -> float | None:
    """Extract mid/last price from a Backpack ticker payload."""
    for row in ticker_rows_from_payload(data, symbol=symbol):
        for key in _PRICE_KEYS:
            raw = row.get(key)
            if raw is None:
                continue
            try:
                px = float(raw)
                if px > 0:
                    return px
            except (TypeError, ValueError):
                continue

        bid = row.get("bidPrice") or row.get("bestBid")
        ask = row.get("askPrice") or row.get("bestAsk")
        try:
            if bid is not None and ask is not None:
                b, a = float(bid), float(ask)
                if b > 0 and a > 0:
                    return (b + a) / 2.0
        except (TypeError, ValueError):
            pass
    return None


def level_price_qty(level: Any) -> tuple[float, float]:
    """Backpack depth level as [price, qty] or {price, quantity}."""
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        return float(level[0]), float(level[1])
    if isinstance(level, dict):
        return float(level.get("price", 0) or 0), float(
            level.get("quantity", level.get("qty", 0)) or 0
        )
    return 0.0, 0.0


def best_bid_ask_from_book(book: dict[str, Any]) -> tuple[float, float] | None:
    """
    Top of book from Backpack depth.

    Backpack levels are not guaranteed sorted — use max(bid) and min(ask).
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None
    bid_prices: list[float] = []
    ask_prices: list[float] = []
    for level in bids:
        price, qty = level_price_qty(level)
        if price > 0 and qty > 0:
            bid_prices.append(price)
    for level in asks:
        price, qty = level_price_qty(level)
        if price > 0 and qty > 0:
            ask_prices.append(price)
    if not bid_prices or not ask_prices:
        return None
    return max(bid_prices), min(ask_prices)


def mid_from_orderbook(book: dict[str, Any]) -> float | None:
    """Mid from depth book (correct best bid / best ask)."""
    top = best_bid_ask_from_book(book)
    if top is None:
        return None
    best_bid, best_ask = top
    if best_bid > 0 and best_ask > 0:
        return (best_bid + best_ask) / 2.0
    return None


def cex_buy_walk_ask_impact_bps(
    book: dict[str, Any],
    usdc_notional: float,
    *,
    max_levels: int = 5,
) -> tuple[float, float, bool]:
    """
    Walk the ask book for ``usdc_notional`` USDC fill.

    Returns ``(impact_bps vs best ask, effective_avg_price, depth_sufficient)``.
    """
    if usdc_notional <= 0:
        return 0.0, 0.0, False

    top = best_bid_ask_from_book(book)
    if top is None:
        return 0.0, 0.0, False
    _, best_ask = top
    if best_ask <= 0:
        return 0.0, 0.0, False

    levels: list[tuple[float, float]] = []
    for level in (book.get("asks") or [])[:max_levels]:
        price, qty = level_price_qty(level)
        if price > 0 and qty > 0:
            levels.append((price, qty))
    levels.sort(key=lambda x: x[0])

    remaining = float(usdc_notional)
    total_base = 0.0
    for price, qty in levels:
        level_usdc = price * qty
        take_usdc = min(remaining, level_usdc)
        total_base += take_usdc / price
        remaining -= take_usdc
        if remaining <= 1e-6:
            break

    if remaining > 1e-4 or total_base <= 0:
        return 999.0, best_ask, False

    eff_price = usdc_notional / total_base
    impact_bps = (eff_price - best_ask) / best_ask * 10_000.0
    return max(0.0, impact_bps), eff_price, True


def cumulative_ask_usdc(book: dict[str, Any], *, max_levels: int = 20) -> float:
    """USDC notional available on ask side (price × base qty), best asks first."""
    asks = book.get("asks") or []
    levels: list[tuple[float, float]] = []
    for level in asks[:max_levels]:
        price, qty = level_price_qty(level)
        if price > 0 and qty > 0:
            levels.append((price, qty))
    levels.sort(key=lambda x: x[0])
    total = 0.0
    for price, qty in levels:
        total += price * qty
    return total
