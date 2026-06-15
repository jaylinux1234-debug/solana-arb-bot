# src/utils/trade_logger.py
"""Backward-compatible re-exports — prefer ``src.execution.trade_logger``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.execution.trade_logger import (
    TRADE_HISTORY_PATH,
    log_real_fill,
    log_trade as _append_trade,
)

__all__ = ["TRADE_HISTORY_PATH", "log_trade", "log_real_fill"]


def log_trade(trade: dict) -> None:
    """Append one trade record with UTC timestamp and profitability flag."""
    profit = float(trade.get("profit_usdc", trade.get("pnl_usdc", 0)) or 0)
    row = {
        **trade,
        "timestamp": datetime.now(UTC).isoformat(),
        "was_profitable": bool(trade.get("was_profitable", profit > 0)),
    }
    _append_trade(row)
