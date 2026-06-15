"""v2 gate checks: dex-cheap only, gross, net, live roundtrip quote."""

from __future__ import annotations

import logging
import os
from typing import Any

from src.dex.jupiter import SOL_MINT
from src.utils.price import bps_diff
from src.v2.config import V2Config
from src.v2.cost_model import CostModel

logger = logging.getLogger(__name__)


def _vol_low_threshold() -> float:
    return float(os.getenv("V2_VOL_LOW_THRESHOLD_PCT", "0.8"))


def adaptive_min_net_bps(
    vol_pct: float,
    inventory_healthy: bool,
    cfg: V2Config | None = None,
) -> float:
    """
    Volatility + inventory adaptive net threshold.

    When Backpack SOL and on-chain USDC are healthy and vol is low, relax net min
    to capture more marginal arbs (env-tunable floor).
    """
    base = float((cfg or V2Config()).min_net_bps_base)
    if not _env_bool("V2_ADAPTIVE_NET_ENABLED", True):
        return base
    if inventory_healthy and float(vol_pct) < _vol_low_threshold():
        relax = float(os.getenv("V2_INVENTORY_NET_RELAX_BPS", "0.1"))
        floor = float(os.getenv("V2_INVENTORY_NET_FLOOR_BPS", "0.15"))
        relaxed = max(floor, base - relax)
        logger.debug(
            "adaptive_min_net_bps | base=%.3f relaxed=%.3f vol=%.2f inventory_ok=True",
            base,
            relaxed,
            vol_pct,
        )
        return relaxed
    return base


def adaptive_min_gross_bps(
    cfg: V2Config,
    vol_pct: float,
    inventory_healthy: bool = False,
) -> float:
    """Scan gross floor: lower required gross when vol is low (and inventory if healthy)."""
    floor = float(os.getenv("V2_DETECT_MIN_GROSS_FLOOR", str(cfg.adaptive_min_gross_floor)))
    scaled = float(cfg.min_gross_bps_base) * (1.0 - float(vol_pct) * 0.5)
    gross = max(floor, scaled)
    if (
        inventory_healthy
        and float(vol_pct) < _vol_low_threshold()
        and _env_bool("V2_ADAPTIVE_GROSS_INVENTORY", True)
    ):
        relax = float(os.getenv("V2_INVENTORY_GROSS_RELAX_BPS", "0.2"))
        gross = max(floor, gross - relax)
    return gross


def adaptive_detect_min_gross(
    cfg: V2Config,
    vol_pct: float,
    inventory_healthy: bool = False,
) -> float:
    """Backward-compatible alias for ``adaptive_min_gross_bps``."""
    return adaptive_min_gross_bps(cfg, vol_pct, inventory_healthy)


def resolve_adaptive_thresholds(
    cfg: V2Config,
    vol_pct: float,
    inventory_healthy: bool,
) -> tuple[float, float]:
    """Return (min_gross_bps, min_net_bps) for detect + static gates."""
    return (
        adaptive_min_gross_bps(cfg, vol_pct, inventory_healthy),
        adaptive_min_net_bps(vol_pct, inventory_healthy, cfg),
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _roundtrip_safety_mult() -> float:
    return float(os.getenv("V2_ROUNDTRIP_NET_SAFETY_MULT", "1.2"))


def _roundtrip_retain_frac() -> float:
    return float(os.getenv("CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC", "0.55"))


def _roundtrip_retain_check_enabled() -> bool:
    return _env_bool("V2_ROUNDTRIP_RETAIN_CHECK", True)


def _roundtrip_probe_micro(size_micro: int) -> int:
    """Quote size for roundtrip sim: trade size by default, optional probe cap."""
    use_trade = _env_bool(
        "V2_ROUNDTRIP_USE_TRADE_SIZE",
        _env_bool("CEX_DEX_ROUNDTRIP_USE_TRADE_SIZE", True),
    )
    if use_trade:
        return max(1, int(size_micro))
    cap = int(os.getenv("V2_ROUNDTRIP_PROBE_USDC_MICRO", "25000000"))
    return max(1, min(int(size_micro), cap))


def _roundtrip_slippage_bps(cfg: V2Config, size_usdc: float) -> int:
    """Dynamic slippage: cost model + size-scaled buffer for live execution."""
    model = CostModel.from_config(cfg)
    modeled = model.get_execution_slippage_bps(size_usdc)
    configured = max(50, int(cfg.execution_slippage_bps))
    size_scale = float(os.getenv("V2_ROUNDTRIP_SLIPPAGE_SIZE_SCALE", "1.5"))
    base_slip = float(
        os.getenv("V2_ROUNDTRIP_SLIPPAGE_BASE_BPS", str(max(60, configured)))
    )
    dynamic = int(base_slip + max(0.0, size_usdc) * size_scale)
    return max(modeled, configured, dynamic)


async def improved_roundtrip_sim(
    jupiter: Any,
    *,
    size_micro: int,
    size_usdc: float,
    cex_bid: float,
    vol_pct: float,
    min_net: float,
    scan_gross_bps: float,
    scan_net: float,
    cfg: V2Config,
    model: CostModel,
    wallet_sol: float = 0.0,
    cex_sol: float = 0.0,
) -> tuple[bool, str, float, dict[str, Any]]:
    """
    Enhanced roundtrip simulation with trade-size quote, dynamic slippage,
    cost-model net, retain check, and safety buffer before execution.
    """
    probe_micro = _roundtrip_probe_micro(size_micro)
    probe_usdc = probe_micro / 1_000_000.0
    slippage = _roundtrip_slippage_bps(cfg, probe_usdc)

    details: dict[str, Any] = {
        "probe_usdc_micro": probe_micro,
        "slippage_bps": slippage,
        "min_net_bps": min_net,
    }

    jup_price, _quote = await jupiter.get_implied_usdc_per_base(
        probe_micro,
        SOL_MINT,
        base_decimals=9,
        slippage_bps=slippage,
        cex_reference=cex_bid,
    )
    if not jup_price or jup_price <= 0:
        details["quote_failed"] = True
        return False, "roundtrip_quote_failed", 0.0, details

    gross_bps = float(bps_diff(float(jup_price), cex_bid))
    if gross_bps <= 0:
        gross_bps = abs((cex_bid - jup_price) / jup_price * 10_000.0)

    net_bps = model.calculate_net_bps(
        gross_bps,
        probe_usdc,
        vol_pct,
        wallet_sol=wallet_sol,
        cex_sol=cex_sol,
    )
    if _env_bool("GO_LIVE_SMALL_ACCOUNT", False):
        safety_mult = float(os.getenv("V2_ROUNDTRIP_NET_SAFETY_MULT_GO_LIVE", "1.05"))
    else:
        safety_mult = _roundtrip_safety_mult()
    safety_min = min_net * safety_mult

    details.update(
        {
            "roundtrip_jup_price": round(float(jup_price), 4),
            "roundtrip_gross_bps": round(gross_bps, 3),
            "roundtrip_net_bps": round(net_bps, 3),
            "scan_net_bps": round(scan_net, 3),
            "scan_gross_bps": round(scan_gross_bps, 3),
            "safety_min_bps": round(safety_min, 3),
            "safety_mult": safety_mult,
        }
    )

    if _roundtrip_retain_check_enabled() and scan_net > 0:
        retain_frac = _roundtrip_retain_frac()
        retain_floor = max(safety_min, scan_net * retain_frac)
        details["retain_floor_bps"] = round(retain_floor, 3)
        if net_bps < retain_floor:
            logger.info(
                "ROUNDTRIP_RETAIN_FAIL | sim=%.3f floor=%.3f scan_net=%.3f retain=%.2f",
                net_bps,
                retain_floor,
                scan_net,
                retain_frac,
            )
            return (
                False,
                f"roundtrip_retain_below_{int(retain_frac * 100)}pct",
                net_bps,
                details,
            )

    if net_bps >= safety_min:
        logger.info(
            "ROUNDTRIP_PASSED | gross=%.2f net=%.3f safety_min=%.3f size=%.2f slippage=%s",
            gross_bps,
            net_bps,
            safety_min,
            probe_usdc,
            slippage,
        )
        return True, "roundtrip_ok", net_bps, details

    if _env_bool("GO_LIVE_SMALL_ACCOUNT", False):
        soft_gross_min = float(os.getenv("V2_ROUNDTRIP_SOFT_SCAN_GROSS_MIN_GO_LIVE", "8.0"))
        soft_net_floor = float(os.getenv("V2_ROUNDTRIP_SOFT_NET_FLOOR_GO_LIVE", "-1.0"))
        soft_factor = float(os.getenv("V2_ROUNDTRIP_SOFT_PASS_FACTOR_GO_LIVE", "0.75"))
    else:
        soft_gross_min = float(os.getenv("V2_ROUNDTRIP_SOFT_SCAN_GROSS_MIN", "12.0"))
        soft_net_floor = float(os.getenv("V2_ROUNDTRIP_SOFT_NET_FLOOR", "-2.0"))
        soft_factor = float(os.getenv("V2_ROUNDTRIP_SOFT_PASS_FACTOR", "0.85"))
    soft_net_min = max(soft_net_floor, min_net * soft_factor)
    details["soft_net_min_bps"] = round(soft_net_min, 3)

    from src.strategies.evaluate_roundtrip import evaluate_roundtrip_cost

    quote_ctx = {
        "gross_bps": gross_bps,
        "vol_pct": vol_pct,
        "scan_gross_bps": scan_gross_bps,
    }
    _model_ok, _model_reason, model_cost = evaluate_roundtrip_cost(
        quote_ctx,
        probe_micro,
        wallet_sol=wallet_sol,
        cex_sol=cex_sol,
    )
    details["model_cost_bps"] = round(model_cost.total_cost_bps, 3)
    details["model_net_bps"] = round(model_cost.net_bps, 3)

    go_live_soft = _model_ok and _model_reason == "roundtrip_soft_pass"

    if go_live_soft or (scan_gross_bps >= soft_gross_min and net_bps >= soft_net_min):
        if _roundtrip_retain_check_enabled() and scan_net > 0 and not go_live_soft:
            retain_frac = _roundtrip_retain_frac()
            soft_retain = max(soft_net_min, scan_net * retain_frac)
            details["soft_retain_floor_bps"] = round(soft_retain, 3)
            if net_bps < soft_retain:
                return False, "roundtrip_soft_retain_fail", net_bps, details
        logger.info(
            "ROUNDTRIP_SOFT_PASS | scan_gross=%.2f roundtrip_gross=%.2f net=%.3f "
            "soft_min=%.3f go_live=%s size=%.2f",
            scan_gross_bps,
            gross_bps,
            net_bps,
            soft_net_min,
            go_live_soft,
            probe_usdc,
        )
        return True, "roundtrip_soft_pass", net_bps, details

    logger.info(
        "ROUNDTRIP_FAILED | scan_gross=%.2f roundtrip_gross=%.2f net=%.3f "
        "safety_min=%.3f scan_net=%.3f",
        scan_gross_bps,
        gross_bps,
        net_bps,
        safety_min,
        scan_net,
    )
    return False, f"roundtrip_net_below_{safety_min:g}", net_bps, details


def _apply_roundtrip_details(
    opportunity: dict[str, Any],
    details: dict[str, Any],
) -> None:
    for key in (
        "roundtrip_jup_price",
        "roundtrip_gross_bps",
        "roundtrip_net_bps",
        "probe_usdc_micro",
        "slippage_bps",
        "safety_min_bps",
        "retain_floor_bps",
    ):
        if key in details:
            opportunity[key if key != "probe_usdc_micro" else "roundtrip_probe_usdc_micro"] = (
                details[key]
            )


def check_static_gates(
    opportunity: dict[str, Any] | None,
    cfg: V2Config,
) -> tuple[bool, str, dict[str, Any]]:
    """Fast gates without extra RPC."""
    if not opportunity:
        return False, "no_signal", {}
    if str(opportunity.get("direction") or "") != "dex_cheap":
        return False, "not_dex_cheap", opportunity

    gross = float(opportunity.get("gross_bps") or 0)
    net = float(opportunity.get("net_bps") or 0)
    min_gross = float(opportunity.get("min_gross_bps") or cfg.min_gross_bps)
    min_net = float(opportunity.get("min_net_bps") or cfg.min_net_bps)
    if gross < min_gross:
        return False, f"gross_below_{min_gross:g}", opportunity
    if net < min_net:
        return False, f"net_below_{min_net:g}", opportunity
    return True, "static_ok", opportunity


async def check_roundtrip_quote(
    jupiter: Any,
    opportunity: dict[str, Any],
    cfg: V2Config,
) -> tuple[bool, str, float]:
    """
    Re-quote Jupiter at trade size with execution slippage and v2.4.2 cost model.

    Uses ``improved_roundtrip_sim`` with safety buffer (min_net * 1.2) and retain
    check vs scan net to reduce false positives before execution.
    """
    size_micro = int(opportunity.get("size_usdc_micro") or cfg.max_trade_usdc_micro)
    size_usdc = float(opportunity.get("size_usdc") or size_micro / 1_000_000.0)
    cex_bid = float(opportunity.get("cex_bid") or 0)
    vol_pct = float(opportunity.get("vol_pct") or 0)
    scan_gross_bps = float(
        opportunity.get("scan_gross_bps") or opportunity.get("gross_bps") or 0
    )
    min_net = float(opportunity.get("min_net_bps") or cfg.min_net_bps)
    scan_net = float(opportunity.get("net_bps") or 0)
    wallet_sol = float(opportunity.get("wallet_sol") or 0.0)
    cex_sol = float(opportunity.get("cex_sol") or 0.0)

    if cex_bid <= 0:
        return False, "no_cex_bid", 0.0

    model = CostModel.from_config(cfg)

    try:
        scan_jup = float(opportunity.get("jup_price") or 0)
        ok, reason, net_bps, details = await improved_roundtrip_sim(
            jupiter,
            size_micro=size_micro,
            size_usdc=size_usdc,
            cex_bid=cex_bid,
            vol_pct=vol_pct,
            min_net=min_net,
            scan_gross_bps=scan_gross_bps,
            scan_net=scan_net,
            cfg=cfg,
            model=model,
            wallet_sol=wallet_sol,
            cex_sol=cex_sol,
        )
        _apply_roundtrip_details(opportunity, details)

        if ok:
            return True, reason, net_bps

        if reason == "roundtrip_quote_failed":
            safety_min = min_net * _roundtrip_safety_mult()
            if (
                scan_jup > 0
                and hasattr(jupiter, "_is_sane_price")
                and jupiter._is_sane_price(scan_jup, cex_bid)
                and scan_net >= safety_min
            ):
                logger.info(
                    "ROUNDTRIP_SCAN_FALLBACK | scan_net=%.3f safety_min=%.3f scan_gross=%.2f",
                    scan_net,
                    safety_min,
                    scan_gross_bps,
                )
                opportunity["roundtrip_jup_price"] = round(scan_jup, 4)
                opportunity["roundtrip_gross_bps"] = round(scan_gross_bps, 3)
                opportunity["roundtrip_net_bps"] = round(scan_net, 3)
                return True, "roundtrip_scan_fallback", scan_net
            return False, "roundtrip_quote_failed", 0.0

        return False, reason, net_bps

    except Exception as exc:
        logger.error("ROUNDTRIP_CHECK_ERROR | %s", exc, exc_info=True)
        return False, "roundtrip_exception", 0.0
