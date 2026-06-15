"""Append live trade rows for ML / analytics (``logs/trade_history.jsonl``)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TRADE_HISTORY_PATH = Path("logs/trade_history.jsonl")


def log_trade(trade: dict) -> None:
    """Append one JSONL trade record."""
    path = TRADE_HISTORY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(trade) + "\n")


def log_blocked_attempt(
    *,
    pair: str,
    gross_bps: float,
    net_bps: float,
    size_usdc: float,
    block_reason: str,
    strategy: str = "cex_dex",
) -> None:
    """Record a gated/blocked live execution attempt (monitoring / ML features)."""
    log_trade(
        {
            "ts": time.time(),
            "strategy": strategy,
            "pair": pair,
            "gross_bps": float(gross_bps),
            "net_bps": float(net_bps),
            "size_usdc": float(size_usdc),
            "success": False,
            "realized_usdc": 0.0,
            "tx_sig": "",
            "execution_attempt": True,
            "block_reason": block_reason,
            "live_fill": False,
            "source": "live_blocked",
        }
    )


def log_execution_trade(
    *,
    pair: str,
    gross_bps: float,
    net_bps: float,
    size_usdc: float,
    success: bool,
    realized_usdc: float,
    tx_sig: str = "",
    trade_id: str = "",
    hops: int = 0,
    strategy: str = "cex_dex",
    extra: dict[str, Any] | None = None,
) -> None:
    """Log one live execution outcome (success or failure) to trade history."""
    row: dict[str, Any] = {
        "ts": time.time(),
        "trade_id": trade_id,
        "strategy": strategy,
        "pair": pair,
        "gross_bps": float(gross_bps),
        "net_bps": float(net_bps),
        "size_usdc": float(size_usdc),
        "success": bool(success),
        "realized_usdc": float(realized_usdc),
        "tx_sig": tx_sig or "",
        "live_fill": True,
        "source": "live",
    }
    if hops:
        row["hops"] = int(hops)
    if extra:
        row.update(extra)
    log_trade(row)


def log_real_fill(
    *,
    trade_id: str,
    gross_bps: float,
    net_bps: float,
    size_usdc_micro: int,
    realized_usdc: float,
    success: bool,
    hops: int = 0,
    pair: str = "SOL/USDC",
    strategy: str = "cex_dex",
    tx_sig: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Structured live fill row for ``src.ml.train_real_fills``."""
    log_execution_trade(
        pair=pair,
        gross_bps=gross_bps,
        net_bps=net_bps,
        size_usdc=int(size_usdc_micro) / 1_000_000.0,
        success=success,
        realized_usdc=realized_usdc,
        tx_sig=tx_sig,
        trade_id=trade_id,
        hops=hops,
        strategy=strategy,
        extra={
            "size_usdc_micro": int(size_usdc_micro),
            "timestamp": datetime.now(UTC).isoformat(),
            "was_profitable": bool(success),
            "profit_usdc": float(realized_usdc),
            **(extra or {}),
        },
    )
