"""
Daily CEX vs prior snapshot and optional on-chain SOL reconciliation.

Configure ``INVENTORY_RECONCILE_ALERT_DELTA_SOL`` (day-over-day CEX SOL change) and
``INVENTORY_RECONCILE_CROSS_ALERT_DELTA_SOL`` (|CEX SOL − on-chain lamports|).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

import src.core.wallet as wallet_safety

logger = logging.getLogger(__name__)


def _state_path() -> Path:
    return Path(os.getenv("INVENTORY_RECONCILE_STATE_PATH", "logs/inventory_reconcile_state.json"))


def _load_state() -> dict[str, Any]:
    p = _state_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(data: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def run_inventory_reconciliation_once(
    client: AsyncClient,
    wallet: Pubkey,
) -> None:
    from src.cex.backpack import get_backpack_client

    cex_sol = float(await get_backpack_client().get_balance("SOL") or 0.0)
    try:
        lamports = (await client.get_balance(wallet)).value
        chain_sol = lamports / 1_000_000_000.0
    except Exception as exc:
        logger.warning("inventory_reconcile: chain balance failed: %s", exc)
        chain_sol = None

    state = _load_state()
    prev_cex = state.get("last_cex_sol")
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    strict_mode = os.getenv("INVENTORY_RECONCILE_STRICT_MODE", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    dod = float(
        os.getenv(
            "INVENTORY_RECONCILE_STRICT_DELTA_SOL"
            if strict_mode
            else "INVENTORY_RECONCILE_ALERT_DELTA_SOL",
            "0.15" if strict_mode else "0.25",
        )
        or 0
    )
    cross = float(
        os.getenv(
            "INVENTORY_RECONCILE_STRICT_CROSS_DELTA_SOL"
            if strict_mode
            else "INVENTORY_RECONCILE_CROSS_ALERT_DELTA_SOL",
            "0.35" if strict_mode else "0",
        )
        or 0
    )

    reconcile_ok = True

    if isinstance(prev_cex, (int, float)) and dod > 0:
        delta = abs(float(cex_sol) - float(prev_cex))
        if delta > dod:
            reconcile_ok = False
            logger.error(
                "INVENTORY RECONCILE ALERT: |Δ CEX SOL day-over-day|=%.6f > threshold %.6f "
                "(prev=%.6f now=%.6f UTC_date=%s strict=%s)",
                delta,
                dod,
                float(prev_cex),
                cex_sol,
                day,
                strict_mode,
            )
            wallet_safety.record_cex_reconciliation(float(cex_sol) - float(prev_cex))

    if chain_sol is not None and cross > 0:
        delta_x = abs(float(cex_sol) - float(chain_sol))
        min_cex_sol = float(os.getenv("INVENTORY_CROSS_MIN_CEX_SOL", "0.1"))
        chain_holding_ok = os.getenv("INVENTORY_ALLOW_CHAIN_SOL_HOLDING", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        ) and float(cex_sol) < min_cex_sol and float(chain_sol) >= min_cex_sol
        if delta_x > cross and not chain_holding_ok:
            logger.error(
                "INVENTORY RECONCILE ALERT: |CEX SOL − on-chain SOL|=%.6f > threshold %.6f "
                "(cex=%.6f chain=%.6f strict=%s)",
                delta_x,
                cross,
                cex_sol,
                chain_sol,
                strict_mode,
            )
            wallet_safety.record_cex_reconciliation(delta_x)
        elif delta_x > cross and chain_holding_ok:
            logger.info(
                "Inventory reconcile: chain SOL holding expected |cex−chain|=%.4f "
                "(cex=%.4f chain=%.4f)",
                delta_x,
                float(cex_sol),
                float(chain_sol),
            )

    if isinstance(prev_cex, (int, float)):
        state["prior_cex_sol"] = float(prev_cex)
    state["last_cex_sol"] = cex_sol
    if chain_sol is not None:
        state["last_chain_sol"] = chain_sol
    state["last_run_utc"] = datetime.now(UTC).isoformat()
    state["last_run_day_utc"] = day
    _save_state(state)
    try:
        from src.monitoring.metrics import set_inventory_reconcile_ok

        set_inventory_reconcile_ok(reconcile_ok)
    except Exception:
        pass

    if reconcile_ok:
        logger.info(
            "Inventory reconcile OK | cex_sol=%.6f chain_sol=%s",
            cex_sol,
            f"{chain_sol:.6f}" if chain_sol is not None else "n/a",
        )
    else:
        logger.warning(
            "Inventory reconcile FAILED thresholds | cex_sol=%.6f chain_sol=%s",
            cex_sol,
            f"{chain_sol:.6f}" if chain_sol is not None else "n/a",
        )


async def daily_inventory_reconciliation_loop(
    client: AsyncClient,
    wallet: Pubkey,
    *,
    first_delay_sec: float = 120.0,
) -> None:
    """Wake periodically (default daily) and run ``run_inventory_reconciliation_once``."""
    interval = max(3600.0, float(os.getenv("INVENTORY_RECONCILE_INTERVAL_SEC", "86400")))
    await asyncio.sleep(max(5.0, first_delay_sec))
    while True:
        try:
            await run_inventory_reconciliation_once(client, wallet)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("inventory_reconcile loop: %s", exc)
        await asyncio.sleep(interval)
