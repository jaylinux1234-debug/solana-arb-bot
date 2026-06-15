"""Shared MEV lane dispatcher (router + multi_strategy_cycle)."""

from __future__ import annotations

import logging
import os
from typing import Any

from src.config.settings import Settings, get_settings
from src.strategies.brain_signals import (
    backrun_signal_present,
    collateral_signal_present,
)

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _collateral_carry_bps(snapshot: dict[str, Any]) -> float:
    col = snapshot.get("collateral_best") if isinstance(snapshot.get("collateral_best"), dict) else {}
    try:
        return float(col.get("net_bps") or col.get("spread_bps") or 0)
    except (TypeError, ValueError):
        return 0.0


def _warn_collateral_idle(snapshot: dict[str, Any], *, lane: str) -> None:
    """Surface high-carry collateral blocks (AI reject / exception) in dispatch logs."""
    carry = _collateral_carry_bps(snapshot)
    min_net = _env_float("COLLATERAL_MIN_NET_BPS", 35.0)
    if carry < min_net:
        return
    logger.warning(
        "MEV dispatch | %s idle despite carry=%.1f bps (min=%.1f) — "
        "check v2_attempts for ai_reject/exception/sim_failed",
        lane,
        carry,
        min_net,
    )


async def execute_mev_lane(
    lane: str,
    snapshot: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> bool:
    """Unified MEV dispatcher — backrun, collateral carry, liquidation."""
    cfg = settings or get_settings()
    snap = snapshot if isinstance(snapshot, dict) else {}

    try:
        if lane == "backrun":
            from src.strategies.backrun_executor import get_backrun_executor

            if not backrun_signal_present(snap):
                return False
            br = snap.get("backrun")
            if not isinstance(br, dict):
                return False
            victim_ctx = {
                **br,
                "tx_sig": br.get("tx_sig") or br.get("signature"),
                "signature": br.get("signature") or br.get("tx_sig"),
            }
            return await get_backrun_executor(cfg).execute(victim_ctx)

        if lane == "collateral_swap":
            from src.strategies.collateral_executor import get_collateral_executor

            if not _env_bool("ENABLE_COLLATERAL_RATE_ARB", True):
                return False
            if not collateral_signal_present(snap):
                return False
            executor = get_collateral_executor(cfg)
            opps = await executor.find_opportunities()
            if not opps:
                return False
            success = await executor.execute(opps[0])
            executor.log_scan(opps, executed=success)
            if not success:
                block = getattr(executor, "_last_block_reason", None)
                if block == "low_usdc":
                    from src.utils.inventory import get_usdc_balance

                    usdc = get_usdc_balance()
                    logger.warning(
                        "MEV dispatch | %s idle despite carry=%.1f bps — "
                        "block=low_usdc (on-chain $%.2f); run v2_withdraw_usdc",
                        lane,
                        _collateral_carry_bps(snap),
                        usdc,
                    )
                else:
                    _warn_collateral_idle(snap, lane=lane)
            return success

        if lane == "liquidation":
            from src.strategies.liquidation_executor import get_liquidation_executor

            if not _env_bool("ENABLE_LIQUIDATION_MONITORING", True):
                return False
            executor = get_liquidation_executor(cfg)
            opps = await executor.scan_opportunities()
            if not opps:
                return False
            success = await executor.execute(opps[0])
            executor.log_scan(opps, executed=success)
            return success

        return False

    except Exception as exc:
        logger.error(
            "MEV dispatch exception | lane=%s err=%s",
            lane,
            str(exc)[:300],
            exc_info=True,
        )
        return False
