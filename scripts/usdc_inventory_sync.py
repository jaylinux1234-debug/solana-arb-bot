#!/usr/bin/env python3
"""
Plan 9 — Top up on-chain USDC from SOL when inventory-first and SOL-heavy.

Runs on a schedule (default every 6h) or manually: ``npm run inventory:usdc-sync``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE_PATH = Path(os.getenv("USDC_SYNC_STATE_PATH", "logs/usdc_inventory_sync.json"))


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


def _read_state() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _due(interval_hours: float) -> bool:
    state = _read_state()
    last = float(state.get("last_run_ts") or 0)
    return (time.time() - last) >= interval_hours * 3600.0


async def _read_chain_balances(*, retries: int = 8) -> tuple[float, float]:
    """Retry on-chain SOL/USDC reads (RPC flakiness caused false skips)."""
    from src.core.capital_preflight import get_ledger_sol_balance
    from src.core.wallet import get_onchain_usdc_balance

    chain_sol = 0.0
    chain_usdc = 0.0
    for attempt in range(max(1, retries)):
        chain_sol = float(await get_ledger_sol_balance())
        chain_usdc = float(await get_onchain_usdc_balance())
        if chain_sol > 0.0 and chain_usdc >= 0.0:
            if chain_sol > 0.0 or chain_usdc > 0.0:
                return chain_sol, chain_usdc
        if attempt + 1 < retries:
            await asyncio.sleep(2.0 * (attempt + 1))
    return chain_sol, chain_usdc


async def run_sync(*, force: bool = False, dry_run: bool = False) -> dict:
    if not _env_bool("CEX_DEX_INVENTORY_FIRST", False) and not force:
        return {"status": "skipped", "reason": "CEX_DEX_INVENTORY_FIRST=false"}

    interval = _env_float("USDC_INVENTORY_SYNC_INTERVAL_HOURS", 6.0)
    if not force and not _due(interval):
        return {"status": "skipped", "reason": "interval_not_elapsed"}

    target_pct = _env_float("USDC_INVENTORY_TARGET_PCT", 30.0) / 100.0
    swap_frac = _env_float("USDC_INVENTORY_SWAP_FRAC", 0.20)
    sol_threshold = _env_float("USDC_INVENTORY_SOL_THRESHOLD", 0.35)

    from src.v2.config import V2Config

    V2Config.from_env().apply_reverse_env()
    from src.config.settings import bootstrap_config
    from src.dex.jupiter import JupiterClient

    settings = bootstrap_config()
    jupiter = JupiterClient(settings)
    rpc_only = _env_bool("V2_REPLENISH_RPC_ONLY", True)

    try:
        return await _run_sync_body(
            jupiter=jupiter,
            force=force,
            dry_run=dry_run,
            target_pct=target_pct,
            swap_frac=swap_frac,
            sol_threshold=sol_threshold,
            rpc_only=rpc_only,
        )
    finally:
        close = getattr(jupiter, "close", None)
        if callable(close):
            await close()


async def _run_sync_body(
    *,
    jupiter: Any,
    force: bool,
    dry_run: bool,
    target_pct: float,
    swap_frac: float,
    sol_threshold: float,
    rpc_only: bool,
) -> dict:
    from src.strategies.adaptive_router import inventory_skew

    chain_sol, chain_usdc = await _read_chain_balances()
    if chain_sol <= 0.0 and chain_usdc <= 0.0:
        return {
            "status": "skipped",
            "reason": "rpc_balance_unavailable",
            "hint": "retry in 30s or run: npm run v2:balance",
        }

    skew_info = inventory_skew(chain_sol=chain_sol, chain_usdc=chain_usdc)
    skew = float(skew_info.get("skew", 0.0))

    v2_target = _env_float(
        "V2_CEX_USDC_WITHDRAW_TARGET",
        _env_float("V2_MIN_USDC_BALANCE", 25.0),
    )
    if force:
        v2_target = max(
            v2_target,
            _env_float("V2_MAX_FLASH_USDC", v2_target),
        )
    force_deficit = max(0.0, v2_target - chain_usdc) if force else 0.0

    if force and force_deficit <= 0:
        return {
            "status": "skipped",
            "reason": "target_met",
            "chain_usdc": chain_usdc,
            "target_usdc": v2_target,
        }

    if skew < sol_threshold and force_deficit <= 0:
        return {
            "status": "skipped",
            "reason": "not_sol_heavy",
            "skew": skew,
            "threshold": sol_threshold,
            "chain_sol": chain_sol,
            "chain_usdc": chain_usdc,
        }

    total_usd = float(skew_info.get("sol_usd", 0)) + float(skew_info.get("usdc", 0))
    target_usdc = (
        chain_usdc + force_deficit
        if force and force_deficit > 0
        else total_usd * target_pct
    )
    deficit = max(0.0, target_usdc - chain_usdc)
    if deficit < _env_float("USDC_INVENTORY_MIN_SWAP_USD", 3.0):
        return {"status": "skipped", "reason": "deficit_below_min", "deficit": deficit}

    swap_usd = min(deficit, float(skew_info.get("sol_usd", 0)) * swap_frac)
    lamports = int(
        swap_usd
        / max(_env_float("USDC_INVENTORY_SOL_USD", 150.0), 1.0)
        * 1_000_000_000
    )
    reserve = int(
        _env_float(
            "V2_SOL_REPLENISH_RESERVE",
            _env_float("CEX_DEX_SOL_SELL_RESERVE_SOL", 0.15),
        )
        * 1_000_000_000
    )
    max_lamports = max(0, int(chain_sol * 1_000_000_000) - reserve)
    lamports = min(lamports, max_lamports)

    if lamports <= 0:
        return {
            "status": "skipped",
            "reason": "no_spendable_sol",
            "chain_sol": chain_sol,
            "chain_usdc": chain_usdc,
            "deficit": deficit,
            "reserve_sol": reserve / 1_000_000_000,
            "hint": "Backpack withdraw (2FA whitelist) or lower V2_SOL_REPLENISH_RESERVE",
        }

    if dry_run:
        return {
            "status": "dry_run",
            "lamports": lamports,
            "swap_usd_est": swap_usd,
            "skew": skew,
        }

    if not await jupiter.has_signing():
        return {"status": "error", "reason": "no_signing"}

    sell = await jupiter.sell_sol(
        amount_lamports=lamports,
        slippage_bps=int(os.getenv("USDC_INVENTORY_SLIPPAGE_BPS", "60")),
        rpc_only=rpc_only,
    )
    out = {
        "status": "ok" if sell.get("success") else "failed",
        "lamports": lamports,
        "tx_sig": sell.get("tx_sig"),
        "skew_before": skew,
        "usdc_before": chain_usdc,
    }
    _write_state({"last_run_ts": time.time(), "last_result": out})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="USDC inventory sync (SOL → USDC)")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run_sync(force=args.force, dry_run=args.dry_run))
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") in ("ok", "skipped", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
