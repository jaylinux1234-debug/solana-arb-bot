"""Roundtrip simulation helpers for strategy facades."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _normalize_size_micro(opportunity: dict[str, Any]) -> int:
    size_raw = opportunity.get("size_usdc") or opportunity.get("size_usdc_micro") or 0
    size_micro = int(size_raw)
    if size_micro > 0 and size_micro < 1_000_000:
        size_micro = int(float(size_raw) * 1_000_000)
    return size_micro


def _fast_cost_model_sim(opportunity: dict[str, Any]) -> dict[str, Any]:
    """
    Aggressive component cost model when only ``gross_bps`` + size are available.
    """
    from src.config.settings import get_settings

    settings = get_settings()
    gross_bps = float(opportunity.get("gross_bps", 0.0))
    size_micro = _normalize_size_micro(opportunity)
    size_usdc = size_micro / 1_000_000.0 if size_micro else 0.0

    base_cost = float(
        getattr(settings, "CEX_DEX_STRATEGY_BASE_COST_BPS", None)
        or _env_float("CEX_DEX_STRATEGY_BASE_COST_BPS", 9.0)
    )
    impact_exp = _env_float("CEX_DEX_SIZE_IMPACT_EXPONENT", 1.12)
    impact_linear = _env_float("CEX_DEX_SIZE_IMPACT_LINEAR_BPS", 2.8)
    size_impact = (size_usdc**impact_exp) * impact_linear if size_usdc > 0 else 0.0
    jito_tip_bps = _env_float("COST_JITO_TIP_BPS", 1.5)
    slippage_bps = _env_float("V2_ROUNDTRIP_SLIPPAGE_BASE_BPS", 60.0)

    total_cost_bps = base_cost + size_impact + jito_tip_bps + slippage_bps
    net_bps = gross_bps - total_cost_bps
    retain_frac = net_bps / gross_bps if gross_bps > 0 else 0.0

    min_net = _env_float(
        "CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS",
        float(getattr(settings, "CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS", 0.25)),
    )
    min_retain = _env_float("CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC", 0.20)
    v2_min_net = _env_float("V2_MIN_NET_BPS", 1.0)

    passed = (
        net_bps >= min_net
        and retain_frac >= min_retain
        and net_bps * 0.85 >= v2_min_net
    )

    result: dict[str, Any] = {
        "passed": passed,
        "gross_bps": round(gross_bps, 2),
        "net_bps": round(net_bps, 2),
        "sim_net_bps": round(net_bps, 2),
        "retain_frac": round(retain_frac, 3),
        "total_cost_bps": round(total_cost_bps, 2),
        "reason": "passed" if passed else "sim_below_threshold",
        "size_usdc": size_micro,
        "mode": "fast_cost_model",
    }

    if passed:
        logger.info("Roundtrip SIM PASSED (fast) | net=%.2f bps", net_bps)
    else:
        logger.debug("Sim blocked (fast): %s", result)

    return result


async def _production_roundtrip_sim(opportunity: dict[str, Any]) -> dict[str, Any]:
    """Jupiter + CEX depth roundtrip pre-sim (production path)."""
    from src.config.settings import get_settings
    from src.dex.jupiter import JupiterClient
    from src.strategies.cex_dex_roundtrip import pre_simulate_cex_buy_dex_sell
    from src.strategies.cex_dex_roundtrip import roundtrip_sim_min_net_bps

    cex_price = float(
        opportunity.get("cex_bid")
        or opportunity.get("cex_buy_price")
        or opportunity.get("cex_ask")
        or 0
    )
    size_micro = _normalize_size_micro(opportunity)

    if cex_price <= 0 or size_micro <= 0:
        return {"passed": False, "reason": "invalid_inputs", "sim_net_bps": 0.0}

    jupiter = JupiterClient(get_settings())
    try:
        ok, sim_net, reason, details = await pre_simulate_cex_buy_dex_sell(
            jupiter,
            size_micro,
            cex_price,
            expected_net_bps=opportunity.get("net_bps"),
        )
        return {
            "passed": ok,
            "reason": reason,
            "sim_net_bps": sim_net,
            "net_bps": sim_net,
            "min_net_bps": roundtrip_sim_min_net_bps(),
            "details": details,
            "mode": "production",
        }
    finally:
        await jupiter.close()


async def roundtrip_simulator(opportunity: dict[str, Any]) -> dict[str, Any]:
    """
    Roundtrip gate for live fills.

    Uses Jupiter + CEX depth when price/size are present; otherwise falls back to
    the aggressive component cost model on ``gross_bps``.
    """
    cex_price = float(
        opportunity.get("cex_bid")
        or opportunity.get("cex_buy_price")
        or opportunity.get("cex_ask")
        or 0
    )
    size_micro = _normalize_size_micro(opportunity)
    gross_bps = float(opportunity.get("gross_bps") or 0.0)

    if cex_price > 0 and size_micro > 0:
        return await _production_roundtrip_sim(opportunity)

    if gross_bps > 0 and size_micro > 0:
        return _fast_cost_model_sim(opportunity)

    return {"passed": False, "reason": "invalid_inputs", "sim_net_bps": 0.0}
