"""CEX price-history volatility gate + adaptive gate tiers (5m window)."""

from __future__ import annotations

import logging
import os
import statistics
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any, Deque

logger = logging.getLogger(__name__)

_WINDOW_SEC = 300.0
_samples: Deque[tuple[float, float]] = deque()
_default_gate: VolatilityGate | None = None


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


def _clamp_vol_pct(vol: float) -> float:
    """Keep adaptive tiers sane (meme prices in the sample buffer caused absurd values)."""
    cap = _env_float("CEX_VOL_MAX_PCT", 50.0)
    return max(0.0, min(cap, float(vol)))


def _normalize_vol_symbol(symbol: str | None) -> str:
    raw = (symbol or "SOL").strip().upper()
    return raw.replace("/USDC", "").split("_")[0]


def _is_sol_vol_price(price: float, symbol: str | None) -> bool:
    if _normalize_vol_symbol(symbol) != "SOL":
        return False
    lo = _env_float("CEX_VOL_SOL_PRICE_MIN", 10.0)
    hi = _env_float("CEX_VOL_SOL_PRICE_MAX", 500.0)
    return lo <= float(price) <= hi


class VolatilityGate:
    """
    Rolling CEX price volatility → adaptive detection gates.

    Volatility is return stdev (%) over recent samples; falls back to range-% when
    too few points exist.
    """

    def __init__(self, backpack_client: Any, jupiter_executor: Any | None = None) -> None:
        self.backpack = backpack_client
        self.jupiter = jupiter_executor
        self.price_history: list[float] = []
        self.last_vol_check: datetime | None = None
        self._cached_vol: float | None = None
        self._cache_sec = _env_float("CEX_VOL_CACHE_SEC", 30.0)
        self._default_low_vol = _env_float("CEX_VOL_DEFAULT_LOW", 0.4)

    async def get_price_history(self, symbol: str = "SOL", *, minutes: float = 5) -> list[float]:
        """Fetch / refresh CEX buy-reference prices for the last ``minutes``."""
        if hasattr(self.backpack, "get_price_history"):
            prices = await self.backpack.get_price_history(symbol, minutes=minutes)
        else:
            prices = await self.backpack.get_recent_prices(symbol, minutes=minutes)
        for p in prices:
            if p and p > 0:
                record_cex_price(float(p))
        return prices

    async def get_5min_volatility(self) -> float:
        """Return volatility % over the last 5 minutes (cached ~30s)."""
        now = datetime.now(UTC)
        if (
            self.last_vol_check
            and self._cached_vol is not None
            and (now - self.last_vol_check).total_seconds() < self._cache_sec
        ):
            return self._cached_vol

        range_vol = get_5m_volatility_pct()
        if range_vol is not None:
            vol = _clamp_vol_pct(range_vol)
            self._cached_vol = vol
            self.last_vol_check = now
            return vol

        prices = await self.get_price_history("SOL", minutes=5)
        if len(prices) < 3:
            vol = _clamp_vol_pct(self._default_low_vol)
            self._cached_vol = vol
            self.last_vol_check = now
            return vol

        self.price_history = [float(p) for p in prices[-20:] if p > 0]
        self.last_vol_check = now
        self._cached_vol = _clamp_vol_pct(self._calculate_vol())
        return self._cached_vol

    def _calculate_vol(self) -> float:
        if len(self.price_history) < 3:
            return self._default_low_vol
        returns = [
            (self.price_history[i] - self.price_history[i - 1]) / self.price_history[i - 1]
            for i in range(1, len(self.price_history))
            if self.price_history[i - 1] > 0
        ]
        if not returns:
            return self._default_low_vol
        return statistics.stdev(returns) * 100.0

    async def get_adaptive_gates(self) -> dict[str, float | str]:
        """
        Tiered gates from 5m volatility.

        - vol > 1.2%: aggressive
        - vol > 0.7%: opportunistic
        - else: strict
        """
        vol = await self.get_5min_volatility()
        aggressive = _env_float("CEX_VOL_AGGRESSIVE_PCT", 1.2)
        opportunistic = _env_float("CEX_VOL_OPPORTUNISTIC_PCT", 0.7)

        if vol > aggressive:
            gates = {
                "gross": _env_float("CEX_VOL_AGG_GROSS_BPS", 6),
                "net": _env_float("CEX_VOL_AGG_NET_BPS", 0),
                "ai": _env_float("CEX_VOL_AGG_AI_CONF", 60),
                "roundtrip_min": _env_float("CEX_VOL_AGG_ROUNDTRIP_MIN_BPS", 0),
                "mode": "aggressive",
            }
        elif vol > opportunistic:
            gates = {
                "gross": _env_float("CEX_VOL_OPP_GROSS_BPS", 7),
                "net": _env_float("CEX_VOL_OPP_NET_BPS", 1),
                "ai": _env_float("CEX_VOL_OPP_AI_CONF", 65),
                "roundtrip_min": _env_float("CEX_VOL_OPP_ROUNDTRIP_MIN_BPS", 1),
                "mode": "opportunistic",
            }
        else:
            gates = {
                "gross": _env_float("CEX_VOL_STRICT_GROSS_BPS", 9),
                "net": _env_float("CEX_VOL_STRICT_NET_BPS", 3),
                "ai": _env_float("CEX_VOL_STRICT_AI_CONF", 70),
                "roundtrip_min": _env_float("CEX_VOL_STRICT_ROUNDTRIP_MIN_BPS", 3),
                "mode": "strict",
            }

        gates["vol_5m"] = vol
        gates["min_gross"] = gates["gross"]
        gates["min_net"] = gates["net"]
        gates["ai_conf"] = gates["ai"]
        return gates


def get_volatility_gate(backpack_client: Any, jupiter_executor: Any | None = None) -> VolatilityGate:
    """Shared gate instance (one per process)."""
    global _default_gate
    if _default_gate is None:
        _default_gate = VolatilityGate(backpack_client, jupiter_executor)
    return _default_gate


def record_cex_price(
    price: float,
    *,
    ts: float | None = None,
    symbol: str | None = "SOL",
) -> None:
    """Append a SOL CEX reference price sample for the 5m vol window."""
    if price <= 0 or not _is_sol_vol_price(price, symbol):
        return
    now = ts if ts is not None else time.time()
    _samples.append((now, float(price)))
    cutoff = now - _WINDOW_SEC
    while _samples and _samples[0][0] < cutoff:
        _samples.popleft()
    if _default_gate is not None and _default_gate.price_history:
        _default_gate.price_history.append(float(price))
        if len(_default_gate.price_history) > 20:
            _default_gate.price_history = _default_gate.price_history[-20:]


def get_recent_prices(*, minutes: float = 5.0) -> list[float]:
    """CEX buy-reference prices recorded in the last ``minutes``."""
    cutoff = time.time() - max(1.0, float(minutes)) * 60.0
    return [float(p) for t, p in _samples if t >= cutoff]


def get_5m_volatility_pct() -> float | None:
    """
    Peak-to-trough range over the last 5 minutes, as percent of mid.

    Returns ``None`` when fewer than two samples exist in the window.
    """
    if len(_samples) < 2:
        return None
    prices = [p for _, p in _samples]
    lo, hi = min(prices), max(prices)
    mid = (lo + hi) / 2.0
    if mid <= 0:
        return None
    return (hi - lo) / mid * 100.0


def should_skip_low_vol_cycle(
    vol_5m_pct: float | None,
    current_gross_bps: float,
    *,
    vol_threshold_pct: float | None = None,
    min_gross_bps: float | None = None,
    best_pair: str | None = None,
) -> bool:
    """
    Skip scan when 5m vol is low and gross edge is not large enough to justify churn.

    ``current_gross_bps`` should be the **max** gross across pairs when multi-pair
    scanning is enabled (not SOL-only).

    Default: ``vol_5m < 0.8%`` and ``gross < 15`` bps.
    """
    if not _env_bool("CEX_DEX_VOL_GATE_ENABLED", True):
        return False
    if vol_5m_pct is None:
        return False

    vol_thresh = (
        vol_threshold_pct
        if vol_threshold_pct is not None
        else _env_float("CEX_DEX_VOL_5M_LOW_THRESHOLD_PCT", 0.8)
    )
    gross_thresh = (
        min_gross_bps
        if min_gross_bps is not None
        else _env_float("CEX_DEX_VOL_SKIP_MAX_GROSS_BPS", 15)
    )
    if vol_5m_pct < vol_thresh and current_gross_bps < gross_thresh:
        pair_note = f" best_pair={best_pair}" if best_pair else ""
        logger.info(
            "Vol gate skip | vol_5m=%.3f%% < %.3f%% gross_max=%.2f < %.2f bps%s",
            vol_5m_pct,
            vol_thresh,
            current_gross_bps,
            gross_thresh,
            pair_note,
        )
        return True
    return False


def oracle_poll_sleep_sec(vol_5m_pct: float | None) -> float:
    """Shorter poll interval when 5m vol indicates high activity."""
    min_s = _env_float("CEX_DEX_ORACLE_POLL_MIN_SEC", 1)
    max_s = _env_float("CEX_DEX_ORACLE_POLL_MAX_SEC", 5)
    high_vol = _env_float("CEX_DEX_HIGH_ACTIVITY_VOL_PCT", 0.8)
    if vol_5m_pct is not None and vol_5m_pct >= high_vol:
        return min_s
    return max_s
