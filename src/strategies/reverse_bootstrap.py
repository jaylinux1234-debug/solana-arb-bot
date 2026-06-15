"""
USDC bootstrap for DEX→CEX reverse lane when ``dex_cheap`` but on-chain USDC is low.

Activates only after at least one recorded ``live_fill`` (Plan 10 / Phase 2 gate).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRADE_LOG = Path(os.getenv("TRADE_HISTORY_PATH", "logs/trade_history.jsonl"))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def count_live_fills(log_path: Path | None = None) -> int:
    path = log_path or TRADE_LOG
    if not path.is_file():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("live_fill") is True or str(row.get("source") or "") == "live_fill":
            n += 1
    return n


def bootstrap_enabled() -> bool:
    if not _env_bool("ENABLE_REVERSE_USDC_BOOTSTRAP", True):
        return False
    min_fills = int(os.getenv("REVERSE_BOOTSTRAP_MIN_LIVE_FILLS", "1"))
    return count_live_fills() >= min_fills


async def maybe_bootstrap_usdc(
    jupiter: Any,
    *,
    wallet_pubkey: str | None = None,
    min_usdc: float | None = None,
    swap_usdc_micro: int | None = None,
) -> dict[str, Any]:
    """Sell a small SOL slice via Jupiter when on-chain USDC is below floor."""
    result: dict[str, Any] = {"status": "skipped", "reason": "disabled"}
    if not bootstrap_enabled():
        result["reason"] = "awaiting_first_live_fill"
        return result

    floor = min_usdc if min_usdc is not None else _env_float("REVERSE_BOOTSTRAP_MIN_USDC", 8.0)
    try:
        from src.core.wallet import get_onchain_usdc_balance

        usdc = float(await get_onchain_usdc_balance())
    except Exception as exc:
        return {"status": "error", "reason": f"usdc_balance:{exc}"}

    if usdc >= floor:
        return {"status": "skipped", "reason": "usdc_sufficient", "usdc": usdc}

    micro = swap_usdc_micro or int(os.getenv("REVERSE_BOOTSTRAP_SWAP_USDC_MICRO", "5000000"))
    if not await jupiter.has_signing():
        return {"status": "error", "reason": "no_signing"}

    try:
        lamports = int(micro * 1_000_000 / max(_env_float("REVERSE_BOOTSTRAP_SOL_USD", 150.0), 1.0))
        lamports = max(lamports, int(os.getenv("REVERSE_BOOTSTRAP_MIN_LAMPORTS", "5000000")))
        sell = await jupiter.sell_sol(
            amount_lamports=lamports,
            slippage_bps=int(os.getenv("REVERSE_BOOTSTRAP_SLIPPAGE_BPS", "80")),
        )
        if sell.get("success"):
            logger.info(
                "Reverse bootstrap | sold %s lamports for USDC (had $%.2f)",
                lamports,
                usdc,
            )
            return {
                "status": "ok",
                "usdc_before": usdc,
                "lamports": lamports,
                "tx_sig": sell.get("tx_sig"),
            }
        return {"status": "failed", "detail": sell}
    except Exception as exc:
        logger.warning("Reverse bootstrap failed: %s", exc)
        return {"status": "error", "reason": str(exc)}
