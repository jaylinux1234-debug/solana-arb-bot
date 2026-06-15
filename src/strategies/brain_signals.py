"""Per-cycle snapshots for the AI strategy brain."""

from __future__ import annotations

import os
import time
from typing import Any

_signals: dict[str, Any] = {
    "liquidation_best": None,
    "collateral_best": None,
    "backrun": None,
    "cex_dex": None,
    "dex_cex_reverse": None,
    "cex_prices": {},
}


def _backrun_ttl_sec() -> float:
    try:
        return float(os.getenv("BACKRUN_SIGNAL_TTL_SEC", "30"))
    except (TypeError, ValueError):
        return 30.0


def backrun_signal_still_valid(ctx: dict[str, Any] | None) -> bool:
    """True when webhook backrun context is active and within TTL."""
    if not isinstance(ctx, dict) or ctx.get("active") is not True:
        return False
    ttl_until = ctx.get("ttl_until")
    if ttl_until is None:
        return True
    try:
        return time.monotonic() < float(ttl_until)
    except (TypeError, ValueError):
        return False


def get_backrun_context() -> dict[str, Any]:
    """Current backrun snapshot (cache TTL + brain merge)."""
    from src.utils.signals import get_backrun_context as _cache_ctx

    ctx = _cache_ctx()
    if ctx.get("active") is True:
        return ctx
    br = _signals.get("backrun")
    if isinstance(br, dict) and backrun_signal_still_valid(br):
        return dict(br)
    return ctx if ctx else {"active": False}


def set_backrun_ttl(ctx: dict[str, Any]) -> None:
    """Persist webhook backrun context with TTL (delegates to utils.signals)."""
    from src.utils.signals import set_backrun_ttl as _set_ttl

    _set_ttl(ctx)
    _signals["backrun"] = get_backrun_context()


def reset_cycle_signals() -> None:
    """Clear per-main-cycle liquidation/collateral hints.

    ``cex_dex`` is updated from its dedicated polling loop and should persist
    between brain ticks. Backrun survives until TTL expires.
    """
    from src.utils.signals import reset_cycle_signals as _reset_ephemeral

    _reset_ephemeral()
    _signals["liquidation_best"] = None
    _signals["collateral_best"] = None
    br = get_backrun_context()
    if br.get("active") is True:
        _signals["backrun"] = br
        return
    _signals["backrun"] = None


def note_cex_dex_best(summary: dict[str, Any]) -> None:
    """Backward-compatible alias for note_cex_dex_context."""
    note_cex_dex_context(summary)


def note_cex_dex_context(summary: dict[str, Any]) -> None:
    """Record latest CEX-DEX snapshot used by the strategy brain."""
    _signals["cex_dex"] = summary


def note_dex_cex_reverse_context(summary: dict[str, Any]) -> None:
    """Record latest DEX→CEX reverse lane snapshot for the cycle brain."""
    _signals["dex_cex_reverse"] = summary


def dex_cex_reverse_signal_present(snapshot: dict | None) -> bool:
    """True when reverse lane has a dex_cheap opportunity (active or directional)."""
    ctx = (snapshot or {}).get("dex_cex_reverse")
    if not isinstance(ctx, dict) or not ctx:
        return False
    if ctx.get("is_dex_cheap") or str(ctx.get("direction", "")).lower() == "dex_cheap":
        return True
    if ctx.get("active") is False:
        return False
    return bool(ctx.get("active"))


def note_liquidation_best(summary: dict[str, Any]) -> None:
    _signals["liquidation_best"] = summary


def note_collateral_best(summary: dict[str, Any]) -> None:
    _signals["collateral_best"] = summary


def note_backrun_context(ctx: dict[str, Any], *, skip_ttl: bool = False) -> None:
    if ctx.get("active") is True and not skip_ttl:
        from src.utils.signals import set_backrun_ttl as _set_ttl

        _set_ttl(ctx)
        stored = get_backrun_context()
        _signals["backrun"] = stored
        return
    if ctx.get("active") is True and "ttl_until" not in ctx:
        ctx = {
            **ctx,
            "ttl_until": time.monotonic() + _backrun_ttl_sec(),
        }
    _signals["backrun"] = ctx


def note_cex_prices(prices: dict[str, float]):
    _signals["cex_prices"] = prices


def note_geyser_pool_update(update: dict[str, Any]) -> None:
    """Latest Geyser/Yellowstone pool account hint for the strategy brain."""
    _signals["geyser_pool"] = update


def brain_snapshot() -> dict:
    """Return current cycle snapshot for AI scoring."""
    return {
        **_signals,
        "triangular_best": _signals.get("triangular_best"),
        "cex_dex_best": _signals.get("cex_dex"),  # legacy key for compatibility
        "cex_prices": _signals.get("cex_prices"),
    }


def _cex_dex_context(snapshot: dict | None) -> dict[str, Any]:
    snap = snapshot or {}
    ctx = snap.get("cex_dex") or snap.get("cex_dex_best") or {}
    return ctx if isinstance(ctx, dict) else {}


def cex_dex_gross_bps_from_snapshot(snapshot: dict | None) -> float | None:
    """Best-effort gross CEX-DEX edge in bps from the latest probe snapshot."""
    cx = _cex_dex_context(snapshot)
    for key in ("gross_bps", "spread_bps_gross", "spread_bps_net", "spread_bps"):
        raw = cx.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _weak_cex_dex_gross_threshold_bps() -> float:
    try:
        return float(os.getenv("BRAIN_CEX_DEX_WEAK_GROSS_BPS", "60"))
    except (TypeError, ValueError):
        return 60.0


def _brain_weak_cex_redirect_enabled() -> bool:
    return os.getenv("BRAIN_CEX_DEX_WEAK_REDIRECT", "true").lower() in ("1", "true", "yes")


def liquidation_signal_present(snapshot: dict | None) -> bool:
    """True when the liquidation lane has a concrete opportunity in the snapshot."""
    liq = (snapshot or {}).get("liquidation_best")
    if not isinstance(liq, dict) or not liq:
        return False
    if liq.get("active") is False:
        return False
    try:
        min_profit = float(
            os.getenv(
                "BRAIN_LIQUIDATION_MIN_PROFIT_USDC",
                os.getenv("LIQUIDATION_MIN_PROFIT_USDC", "3.5"),
            )
        )
    except (TypeError, ValueError):
        min_profit = 3.5
    try:
        profit = float(liq.get("profit_usdc") or 0.0)
    except (TypeError, ValueError):
        profit = 0.0
    return profit >= min_profit


def backrun_signal_present(snapshot: dict | None) -> bool:
    """True when Helius backrun lane is enabled and a recent large swap was seen."""
    br = (snapshot or {}).get("backrun")
    if not isinstance(br, dict) or not br:
        return False
    if not backrun_signal_still_valid(br):
        return False
    if os.getenv("ENABLE_HELIUS_WEBHOOK_BACKRUN", "false").lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    try:
        min_amt = int(os.getenv("HELIUS_BACKRUN_MIN_AMOUNT_MICRO", "50000000"))
    except (TypeError, ValueError):
        min_amt = 50_000_000
    try:
        amt = int(br.get("amount_micro") or 0)
    except (TypeError, ValueError):
        amt = 0
    return amt >= min_amt


def lane_signal_present(snapshot: dict | None, lane: str) -> bool:
    if lane == "liquidation":
        return liquidation_signal_present(snapshot)
    if lane == "collateral_swap":
        return collateral_signal_present(snapshot)
    if lane == "backrun":
        return backrun_signal_present(snapshot)
    if lane == "cex_dex":
        cx = _cex_dex_context(snapshot)
        return bool(cx.get("active"))
    return False


def next_strategy_in_priority(
    snapshot: dict | None,
    *,
    after: str = "cex_dex",
) -> str | None:
    """
    First strategy after ``after`` in ``STRATEGY_PRIORITY_ORDER`` with a live snapshot signal.

    Used when CEX-DEX gross is weak — try collateral, liquidation, or backrun in order.
    """
    from src.utils.ai import _parse_strategy_priority_order

    order = _parse_strategy_priority_order()
    try:
        start = order.index(after) + 1
    except ValueError:
        start = 0

    for lane in order[start:]:
        if lane == after:
            continue
        if lane_signal_present(snapshot, lane):
            return lane

    for lane in order:
        if lane == after:
            continue
        if lane_signal_present(snapshot, lane):
            return lane
    return None


def collateral_signal_present(snapshot: dict | None) -> bool:
    """True when collateral_swap lane has net carry above threshold."""
    col = (snapshot or {}).get("collateral_best")
    if not isinstance(col, dict) or not col:
        return False
    if col.get("active") is True:
        return True
    try:
        min_net = float(
            os.getenv(
                "COLLATERAL_MIN_NET_BPS",
                os.getenv(
                    "COLLATERAL_MIN_SPREAD_BPS",
                    os.getenv("BRAIN_COLLATERAL_MIN_SPREAD_BPS", "35"),
                ),
            )
        )
    except (TypeError, ValueError):
        min_net = 35.0
    try:
        net_bps = float(col.get("net_bps") or col.get("spread_bps") or 0.0)
    except (TypeError, ValueError):
        net_bps = 0.0
    return net_bps >= min_net


def preferred_lane_when_weak_cex_dex(snapshot: dict | None) -> str | None:
    """
    When CEX-DEX gross edge is below ``BRAIN_CEX_DEX_WEAK_GROSS_BPS``, pick the next
    strategy in ``STRATEGY_PRIORITY_ORDER`` (collateral, liquidation, backrun, …).
    """
    if not _brain_weak_cex_redirect_enabled():
        return None

    gross = cex_dex_gross_bps_from_snapshot(snapshot)
    if gross is None:
        return None
    if gross >= _weak_cex_dex_gross_threshold_bps():
        return None

    return next_strategy_in_priority(snapshot, after="cex_dex")


def apply_weak_cex_dex_score_bias(
    scores: dict[str, float],
    snapshot: dict | None,
) -> tuple[str | None, dict[str, float]]:
    """
    When CEX-DEX gross bps is weak, boost liquidation/collateral and demote cex_dex.

    Returns ``(forced_lane, adjusted_scores)`` when redirect applies; else ``(None, scores)``.
    """
    lane = preferred_lane_when_weak_cex_dex(snapshot)
    if lane is None:
        return None, dict(scores)

    adjusted = {k: float(v) for k, v in scores.items()}
    try:
        win_floor = float(os.getenv("STRATEGY_WIN_THRESHOLD", "28"))
    except (TypeError, ValueError):
        win_floor = 28.0

    adjusted["cex_dex"] = min(adjusted.get("cex_dex", 12.0), max(0.0, win_floor - 1.0))
    adjusted[lane] = max(adjusted.get(lane, 0.0), win_floor + 10.0, 72.0)
    if lane == "liquidation" and collateral_signal_present(snapshot):
        adjusted["collateral_swap"] = max(
            adjusted.get("collateral_swap", 0.0),
            win_floor,
        )

    return lane, adjusted
