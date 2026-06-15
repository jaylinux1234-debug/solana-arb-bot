"""Shared AdvancedCostModel roundtrip evaluation (CEX-DEX + v2)."""

from __future__ import annotations

import logging
import os
from typing import Any

from src.core.cost_model import AdvancedCostModel, RoundtripCost, get_advanced_cost_model

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


def roundtrip_min_net_bps() -> float:
    if _env_bool("GO_LIVE_SMALL_ACCOUNT", False):
        go_live_raw = os.getenv("CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS_GO_LIVE", "").strip()
        if go_live_raw:
            return float(go_live_raw)
    raw = os.getenv("CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS", "").strip()
    if raw:
        return float(raw)
    return _env_float("CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS", 0.5)


def roundtrip_retain_frac() -> float:
    if _env_bool("GO_LIVE_SMALL_ACCOUNT", False):
        return _env_float(
            "CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC_GO_LIVE",
            _env_float("CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC", 0.18),
        )
    return _env_float("CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC", 0.18)


def roundtrip_soft_pass_factor() -> float:
    if _env_bool("GO_LIVE_SMALL_ACCOUNT", False):
        return _env_float(
            "CEX_DEX_ROUNDTRIP_SOFT_PASS_FACTOR_GO_LIVE",
            _env_float("CEX_DEX_ROUNDTRIP_SOFT_PASS_FACTOR", 0.80),
        )
    return _env_float("CEX_DEX_ROUNDTRIP_SOFT_PASS_FACTOR", 0.80)


def min_gross_for_roundtrip() -> float:
    return _env_float("CEX_DEX_MIN_GROSS_SPREAD_BPS", 6.0)


def _trade_usdc_dollars(size_usdc: int | float) -> float:
    raw = float(size_usdc)
    if raw >= 1_000_000:
        return raw / 1_000_000.0
    return raw


def should_execute_roundtrip(
    signal: dict[str, Any],
    *,
    wallet_sol: float = 0.0,
    cex_sol: float = 0.0,
    model: AdvancedCostModel | None = None,
) -> tuple[bool, RoundtripCost]:
    """PATH-aware model gate: strong pass + GO_LIVE soft-pass."""
    gross = float(signal.get("gross_bps") or 0.0)
    size_usdc = signal.get("size_usdc") or signal.get("size_usdc_micro") or 25_000_000

    m = model or get_advanced_cost_model()
    vol = float(
        signal.get("vol")
        or signal.get("vol_pct")
        or signal.get("vol_5m_pct")
        or 0.8
    )
    cost = m.calculate_roundtrip(
        gross_bps=gross,
        trade_usdc=size_usdc,
        vol_5m_pct=vol,
        wallet_sol=float(wallet_sol or signal.get("wallet_sol") or 0.0),
        cex_sol=float(cex_sol or signal.get("cex_sol") or 0.0),
        is_reverse_path=True,
    )
    logger.info(
        "Cost breakdown for $%.1fUSDC: %s | net=%.2fbps",
        _trade_usdc_dollars(size_usdc),
        cost.breakdown,
        cost.net_bps,
    )

    min_net = roundtrip_min_net_bps()
    soft_factor = roundtrip_soft_pass_factor()

    if cost.net_bps >= min_net:
        return True, cost
    if _env_bool("GO_LIVE_SMALL_ACCOUNT", False) and cost.net_bps >= min_net * soft_factor:
        logger.info("SOFT_PASS: marginal but acceptable for small account")
        return True, cost
    return False, cost


def evaluate_roundtrip_cost(
    quote_data: dict[str, Any],
    size_usdc: int | float,
    *,
    wallet_sol: float = 0.0,
    cex_sol: float = 0.0,
    model: AdvancedCostModel | None = None,
) -> tuple[bool, str, RoundtripCost]:
    """
    Model-based roundtrip gate with strong pass + GO_LIVE soft-pass.

    Returns ``(ok, reason, cost)``.
    """
    gross_bps = float(quote_data.get("gross_bps") or 0.0)
    if gross_bps < min_gross_for_roundtrip():
        return False, "gross_below_min", RoundtripCost(gross_bps=gross_bps)

    signal = {
        **quote_data,
        "gross_bps": gross_bps,
        "size_usdc": size_usdc,
        "size_usdc_micro": size_usdc,
    }
    ok, cost = should_execute_roundtrip(
        signal,
        wallet_sol=wallet_sol,
        cex_sol=cex_sol,
        model=model,
    )
    min_net = roundtrip_min_net_bps()
    if ok:
        reason = (
            "roundtrip_soft_pass"
            if _env_bool("GO_LIVE_SMALL_ACCOUNT", False) and cost.net_bps < min_net
            else "roundtrip_strong"
        )
        if reason == "roundtrip_strong":
            logger.info(
                "Strong roundtrip | net=%.2fbps gross=%.2f cost=%.2f",
                cost.net_bps,
                gross_bps,
                cost.total_cost_bps,
            )
        return True, reason, cost

    return False, f"roundtrip_net_below_{min_net:g}", cost


def log_roundtrip_near_miss(
    quote_data: dict[str, Any],
    cost: RoundtripCost,
    *,
    lane: str = "cex_dex",
) -> None:
    try:
        from src.monitoring.metrics import record_cex_dex_near_miss

        record_cex_dex_near_miss(
            float(quote_data.get("gross_bps") or 0.0),
            reason=f"roundtrip_sim:net_{cost.net_bps:.2f}",
        )
    except Exception:
        pass
    logger.info(
        "ROUNDTRIP_NEAR_MISS | lane=%s gross=%.2f net=%.2f cost=%.2f",
        lane,
        cost.gross_bps,
        cost.net_bps,
        cost.total_cost_bps,
    )
