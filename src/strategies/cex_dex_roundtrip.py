"""Jupiter round-trip simulation helpers for CEX-DEX execution gates."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from src.config.settings import get_settings
from src.dex.jupiter import SOL_MINT, USDC_MINT
from src.dex.jupiter_params import quote_route_hops, resolve_slippage_bps

if TYPE_CHECKING:
    from src.dex.jupiter import JupiterClient

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def roundtrip_sim_use_trade_size() -> bool:
    """When true, pre-sim uses max(probe, trade) micro-USDC (align detect with execute)."""
    return _env_bool("CEX_DEX_ROUNDTRIP_USE_TRADE_SIZE", True)


def effective_roundtrip_usdc_micro(
    probe_usdc_micro: int,
    trade_usdc_micro: int,
) -> int:
    """Size for Jupiter roundtrip quotes (never below probe when trade-size mode on)."""
    probe = max(0, int(probe_usdc_micro))
    trade = max(0, int(trade_usdc_micro))
    if roundtrip_sim_use_trade_size():
        return max(probe, trade)
    return trade if trade > 0 else probe


def roundtrip_sim_min_net_bps() -> float:
    """Minimum quote-based net bps for CEX-buy → Jupiter-sell pre-sim."""
    raw = os.getenv("CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS", "").strip()
    if raw:
        return float(raw)
    if _env_bool("GO_LIVE_SMALL_ACCOUNT", False):
        return float(os.getenv("CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS_GO_LIVE", "1"))
    cfg = get_settings()
    return float(getattr(cfg, "CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS", 3.0))


def roundtrip_sim_retain_check_enabled() -> bool:
    return _env_bool("CEX_DEX_ROUNDTRIP_SIM_RETAIN_CHECK", True)


def roundtrip_soft_pass_factor() -> float:
    """Fraction of ``min_net`` for near-miss pass (e.g. 0.85 → 0.8 bps when min is 1.0)."""
    return float(os.getenv("CEX_DEX_ROUNDTRIP_SOFT_PASS_FACTOR", "0.85"))


def roundtrip_net_gate_passes(
    net_bps: float,
    min_net_bps: float,
) -> tuple[bool, bool]:
    """
    Softer roundtrip net gate.

    Returns ``(passes, near_miss_ok)`` — ``near_miss_ok`` when net is in the soft band
    ``[min * factor, min)``.
    """
    min_net = float(min_net_bps)
    if net_bps >= min_net:
        return True, False
    soft_min = min_net * roundtrip_soft_pass_factor()
    if net_bps >= soft_min:
        logger.info(
            "ROUNDTRIP_NEAR_MISS_OK | net_bps=%.2f original_threshold=%.2f soft_min=%.2f",
            net_bps,
            min_net,
            soft_min,
        )
        return True, True
    return False, False


def get_effective_cost_bps(
    size_usdc_micro: int,
    *,
    vol_5m_pct: float = 0.8,
    wallet_sol: float = 0.0,
    cex_sol: float = 0.0,
) -> float:
    """Dynamic modeled drag (bps) from advanced cost model or legacy env tier."""
    from src.core.cost_model import get_advanced_cost_model, use_advanced_cost_model

    if use_advanced_cost_model():
        return get_advanced_cost_model().total_cost_bps(
            int(size_usdc_micro),
            vol_5m_pct=vol_5m_pct,
            wallet_sol=wallet_sol,
            cex_sol=cex_sol,
            is_reverse_path=True,
        )

    cfg = get_settings()
    base = float(
        os.getenv(
            "CEX_DEX_STRATEGY_BASE_COST_BPS",
            str(getattr(cfg, "CEX_DEX_STRATEGY_BASE_COST_BPS", 14)),
        )
    )
    large_micro = int(os.getenv("CEX_DEX_LARGE_TRADE_USDC_MICRO", "15000000"))
    extra_bps = float(os.getenv("CEX_DEX_LARGE_SIZE_EXTRA_COST_BPS", "3"))
    if int(size_usdc_micro) > large_micro:
        base += extra_bps
    return base


def _roundtrip_use_cex_depth() -> bool:
    return _env_bool("CEX_DEX_ROUNDTRIP_USE_DEPTH", True)


def _roundtrip_include_jito_tip() -> bool:
    return _env_bool("CEX_DEX_ROUNDTRIP_INCLUDE_JITO_TIP", True)


async def _cex_buy_price_from_depth(
    usdc_notional: float,
    cex_price: float,
    *,
    backpack_symbol: str = "SOL_USDC",
) -> tuple[float, dict[str, Any], str | None]:
    """Backpack ask walk for effective buy price; optional depth gate."""
    meta: dict[str, Any] = {}
    if not _roundtrip_use_cex_depth() or usdc_notional <= 0:
        return cex_price, meta, None

    try:
        from src.cex.backpack import get_backpack_client
        from src.cex.backpack_ticker import cex_buy_walk_ask_impact_bps

        levels = int(os.getenv("CEX_DEX_ROUNDTRIP_DEPTH_LEVELS", "5"))
        market = (backpack_symbol or "SOL_USDC").strip().upper()
        book = await get_backpack_client().get_orderbook(market, limit=levels)
        impact_bps, eff_price, sufficient = cex_buy_walk_ask_impact_bps(
            book,
            usdc_notional,
            max_levels=levels,
        )
        meta["cex_depth_market"] = market
        meta["cex_depth_impact_bps"] = round(impact_bps, 2)
        meta["cex_effective_ask"] = round(eff_price, 6)
        if not sufficient:
            return cex_price, meta, "cex_depth_insufficient"
        return eff_price, meta, None
    except Exception as exc:
        logger.debug("roundtrip CEX depth skipped: %s", exc)
        return cex_price, meta, None


def _estimate_jito_tip_bps(
    net_bps: float,
    size_usdc_micro: int,
    *,
    sol_price_usd: float,
) -> float:
    if not _roundtrip_include_jito_tip() or size_usdc_micro <= 0:
        return 0.0
    try:
        from src.execution.jito_bundle import resolve_jito_tip_lamports

        tip_lam = resolve_jito_tip_lamports(
            net_bps=float(net_bps),
            size_usdc_micro=int(size_usdc_micro),
        )
        trade_usdc = size_usdc_micro / 1_000_000.0
        tip_usdc = (tip_lam / 1_000_000_000.0) * max(sol_price_usd, 1.0)
        return (tip_usdc / trade_usdc) * 10_000.0
    except Exception as exc:
        logger.debug("jito tip bps estimate skipped: %s", exc)
        return 0.0


def _quote_price_impact_bps(quote: dict[str, Any]) -> float | None:
    pct = quote.get("priceImpactPct")
    if pct is None:
        return None
    try:
        return abs(float(pct)) * 10_000.0
    except (TypeError, ValueError):
        return None


async def pre_simulate_cex_buy_dex_sell(
    jupiter: JupiterClient,
    usdc_micro: int,
    cex_price: float,
    *,
    backpack_symbol: str = "SOL_USDC",
    base_mint: str = SOL_MINT,
    base_decimals: int = 9,
    slippage_bps: int | None = None,
    expected_net_bps: float | None = None,
    probe_usdc_micro: int | None = None,
    min_net_bps: float | None = None,
) -> tuple[bool, float, str, dict[str, Any]]:
    """
    Model CEX market buy (USDC → base) then Jupiter sell (base → USDC).

    Returns ``(ok, modeled_net_bps, reason, details)``.
    """
    if usdc_micro <= 0 or cex_price <= 0:
        return False, 0.0, "invalid_inputs", {}

    sim_micro = effective_roundtrip_usdc_micro(
        int(probe_usdc_micro or 0),
        int(usdc_micro),
    )
    effective_cost_bps = get_effective_cost_bps(sim_micro)

    bps = slippage_bps if slippage_bps is not None else resolve_slippage_bps(
        base_mint, USDC_MINT
    )
    usdc = sim_micro / 1_000_000.0
    eff_cex_price, depth_meta, depth_err = await _cex_buy_price_from_depth(
        usdc,
        cex_price,
        backpack_symbol=backpack_symbol,
    )
    if depth_err:
        details = {"cex_price": cex_price, **depth_meta}
        return False, 0.0, depth_err, details

    cex_fee_fudge = float(os.getenv("CEX_DEX_CEX_BUY_FILL_FUDGE", "0.995"))
    base_raw = int(
        (usdc / eff_cex_price) * (10**int(base_decimals)) * cex_fee_fudge
    )
    base_raw = max(base_raw, 1)

    sell_quote = await jupiter.fetch_quote_raw(
        base_raw,
        input_mint=base_mint,
        output_mint=USDC_MINT,
        slippage_bps=bps,
    )
    if not sell_quote or "outAmount" not in sell_quote:
        return False, 0.0, "sell_quote_failed", {}

    usdc_back_micro = int(sell_quote["outAmount"])
    gross_net_bps = ((usdc_back_micro - sim_micro) / sim_micro) * 10_000.0
    tip_bps = _estimate_jito_tip_bps(gross_net_bps, sim_micro, sol_price_usd=eff_cex_price)
    net_bps = gross_net_bps - tip_bps
    min_net = float(min_net_bps) if min_net_bps is not None else roundtrip_sim_min_net_bps()
    impact_bps = _quote_price_impact_bps(sell_quote)

    details: dict[str, Any] = {
        "base_mint": base_mint,
        "base_amount_raw": base_raw,
        "usdc_in_micro": sim_micro,
        "usdc_requested_micro": int(usdc_micro),
        "usdc_back_micro": usdc_back_micro,
        "cex_price": cex_price,
        "cex_effective_buy_price": round(eff_cex_price, 6),
        "route_hops": quote_route_hops(sell_quote),
        "slippage_bps": bps,
        "effective_cost_bps": round(effective_cost_bps, 2),
        "gross_sim_net_bps": round(gross_net_bps, 2),
        "jito_tip_bps_est": round(tip_bps, 2),
        "sim_net_bps": round(net_bps, 2),
        "min_net_bps": min_net,
        "expected_net_bps": expected_net_bps,
        **depth_meta,
    }
    if impact_bps is not None:
        details["sell_price_impact_bps"] = round(impact_bps, 2)

    passes, near_miss_ok = roundtrip_net_gate_passes(net_bps, min_net)
    details["roundtrip_near_miss_ok"] = near_miss_ok
    details["roundtrip_soft_min_bps"] = round(min_net * roundtrip_soft_pass_factor(), 3)
    if not passes:
        logger.info(
            "ROUNDTRIP_PRE_SIM reject | net=%.2fbps min=%.1f expected=%s hops=%s",
            net_bps,
            min_net,
            expected_net_bps,
            details.get("route_hops"),
        )
        return False, net_bps, f"net_below_{min_net:.1f}bps", details

    if (
        roundtrip_sim_retain_check_enabled()
        and expected_net_bps is not None
        and expected_net_bps > 0
    ):
        retain = float(os.getenv("CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC", "0.55"))
        if _env_bool("GO_LIVE_SMALL_ACCOUNT", False):
            retain = float(
                os.getenv(
                    "CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC_GO_LIVE",
                    os.getenv("CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC", "0.35"),
                )
            )
        floor = max(min_net, float(expected_net_bps) * retain)
        soft_floor = floor * roundtrip_soft_pass_factor()
        details["modeled_retain_floor_bps"] = round(floor, 2)
        details["modeled_retain_soft_floor_bps"] = round(soft_floor, 2)
        if net_bps < soft_floor:
            logger.info(
                "ROUNDTRIP_PRE_SIM reject | sim=%.2fbps below retain soft %.2f (modeled=%.1f)",
                net_bps,
                soft_floor,
                expected_net_bps,
            )
            return (
                False,
                net_bps,
                f"sim_below_{int(retain * 100)}pct_modeled",
                details,
            )
        if net_bps < floor:
            logger.info(
                "ROUNDTRIP_NEAR_MISS_OK retain | sim=%.2fbps floor=%.2f soft=%.2f modeled=%.1f",
                net_bps,
                floor,
                soft_floor,
                expected_net_bps,
            )
            details["roundtrip_retain_near_miss_ok"] = True

    logger.info(
        "ROUNDTRIP_PRE_SIM ok | net=%.2fbps min=%.1f hops=%s impact=%s",
        net_bps,
        min_net,
        details.get("route_hops"),
        impact_bps,
    )
    return True, net_bps, "ok", details


async def pre_simulate_full_jupiter_roundtrip(
    jupiter: JupiterClient,
    usdc_micro: int,
    *,
    slippage_bps: int | None = None,
) -> tuple[bool, float, str]:
    """USDC→SOL→USDC on Jupiter only (sanity check / aggressive pre-sim)."""
    if usdc_micro <= 0:
        return False, 0.0, "invalid_size"

    bps = slippage_bps if slippage_bps is not None else resolve_slippage_bps(
        USDC_MINT, SOL_MINT
    )
    buy = await jupiter.fetch_quote_raw(
        usdc_micro,
        input_mint=USDC_MINT,
        output_mint=SOL_MINT,
        slippage_bps=bps,
    )
    if not buy or "outAmount" not in buy:
        return False, 0.0, "buy_quote_failed"

    sol_out = int(buy["outAmount"])
    sell = await jupiter.fetch_quote_raw(
        sol_out,
        input_mint=SOL_MINT,
        output_mint=USDC_MINT,
        slippage_bps=bps,
    )
    if not sell or "outAmount" not in sell:
        return False, 0.0, "sell_quote_failed"

    back = int(sell["outAmount"])
    net_bps = ((back - usdc_micro) / usdc_micro) * 10_000.0
    min_net = float(os.getenv("CEX_DEX_JUPITER_ROUNDTRIP_MIN_NET_BPS", "-25"))
    if net_bps < min_net:
        return False, net_bps, f"jup_roundtrip_{net_bps:.1f}bps"
    return True, net_bps, "ok"
