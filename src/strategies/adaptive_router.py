"""
Adaptive Lane Router — scores lanes using recent fill rate, volatility, and inventory skew.

Decoupled from :mod:`src.strategies.brain` so routing policy can evolve independently.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRADE_LOG = Path(os.getenv("TRADE_HISTORY_PATH", "logs/trade_history.jsonl"))
LANES = ("cex_dex", "dex_cex_reverse", "backrun", "collateral_swap", "liquidation")


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


def router_window_hours() -> float:
    return _env_float("ADAPTIVE_ROUTER_WINDOW_HOURS", 4.0)


def _row_ts(row: dict[str, Any]) -> datetime | None:
    raw = row.get("timestamp")
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    ts = row.get("ts")
    if ts is not None:
        try:
            return datetime.fromtimestamp(float(ts), tz=UTC)
        except (TypeError, ValueError, OSError):
            return None
    return None


def _is_fill(row: dict[str, Any]) -> bool:
    return row.get("live_fill") is True or str(row.get("source") or "") == "live_fill"


def load_recent_trades(
    *,
    hours: float | None = None,
    log_path: Path | None = None,
) -> list[dict[str, Any]]:
    path = log_path or TRADE_LOG
    if not path.is_file():
        return []
    cutoff = datetime.now(UTC) - timedelta(hours=hours or router_window_hours())
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        dt = _row_ts(row)
        if dt is None or dt >= cutoff:
            rows.append(row)
    return rows


def lane_fill_rates(
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, float]:
    """Fill rate per lane in [0, 1] (0 when no attempts)."""
    data = rows if rows is not None else load_recent_trades()
    attempts: dict[str, int] = defaultdict(int)
    fills: dict[str, int] = defaultdict(int)
    for row in data:
        lane = str(row.get("strategy") or "cex_dex")
        if lane not in LANES:
            lane = "cex_dex"
        if not row.get("execution_attempt") and not _is_fill(row):
            continue
        attempts[lane] += 1
        if _is_fill(row):
            fills[lane] += 1
    return {
        lane: (fills[lane] / attempts[lane] if attempts[lane] else 0.0) for lane in LANES
    }


def inventory_skew(
    *,
    chain_sol: float | None = None,
    chain_usdc: float | None = None,
    sol_price_usd: float | None = None,
) -> dict[str, float]:
    """
    Skew in [-1, 1]: negative = USDC-heavy, positive = SOL-heavy (by USD notional).
    """
    sol = float(chain_sol or 0.0)
    usdc = float(chain_usdc or 0.0)
    px = float(sol_price_usd or _env_float("ADAPTIVE_ROUTER_SOL_USD_FALLBACK", 150.0))
    sol_usd = sol * px
    total = sol_usd + usdc
    if total <= 0:
        return {"skew": 0.0, "sol_usd": 0.0, "usdc": usdc}
    skew = (sol_usd - usdc) / total
    return {"skew": max(-1.0, min(1.0, skew)), "sol_usd": sol_usd, "usdc": usdc}


async def fetch_inventory_skew(backpack: Any | None = None) -> dict[str, float]:
    """Best-effort on-chain + optional CEX balances."""
    chain_sol = 0.0
    chain_usdc = 0.0
    sol_price = _env_float("ADAPTIVE_ROUTER_SOL_USD_FALLBACK", 150.0)
    try:
        from src.core.capital_preflight import get_ledger_sol_balance

        chain_sol = float(await get_ledger_sol_balance())
    except Exception as exc:
        logger.debug("adaptive_router chain SOL: %s", exc)
    try:
        from src.core.wallet import get_onchain_usdc_balance

        chain_usdc = float(await get_onchain_usdc_balance())
    except Exception as exc:
        logger.debug("adaptive_router chain USDC: %s", exc)
    if backpack is not None:
        try:
            buy, _, _ = await backpack.get_cex_buy_reference_price("SOL_USDC")
            if buy and buy > 0:
                sol_price = float(buy)
        except Exception:
            pass
    return inventory_skew(chain_sol=chain_sol, chain_usdc=chain_usdc, sol_price_usd=sol_price)


def route_scores(
    base_scores: dict[str, float],
    *,
    vol_5m_pct: float,
    fill_rates: dict[str, float] | None = None,
    inv_skew: float = 0.0,
    snapshot: dict | None = None,
) -> dict[str, float]:
    """
    Blend base brain scores with adaptive signals.

    - Fill rate bonus: up to +15 per lane (4h window)
    - Vol: opportunistic boost to cex_dex / reverse when vol > 0.7%
    - Inventory: SOL-heavy → dex_cex_reverse; USDC-heavy → cex_dex
    """
    rates = fill_rates if fill_rates is not None else lane_fill_rates()
    fill_weight = _env_float("ADAPTIVE_ROUTER_FILL_WEIGHT", 15.0)
    vol_weight = _env_float("ADAPTIVE_ROUTER_VOL_WEIGHT", 8.0)
    skew_weight = _env_float("ADAPTIVE_ROUTER_SKEW_WEIGHT", 12.0)

    out: dict[str, float] = {}
    for lane in LANES:
        base = float(base_scores.get(lane, base_scores.get(lane.replace("_", "-"), 0)) or 0)
        score = base
        score += rates.get(lane, 0.0) * fill_weight

        if vol_5m_pct >= 0.7 and lane in ("cex_dex", "dex_cex_reverse"):
            score += vol_weight * min(2.0, vol_5m_pct / 0.7)

        if lane == "dex_cex_reverse" and inv_skew > 0.25:
            score += skew_weight * inv_skew
        if lane == "cex_dex" and inv_skew < -0.15:
            score += skew_weight * abs(inv_skew)

        if snapshot and lane == "dex_cex_reverse":
            from src.strategies.brain_signals import dex_cex_reverse_signal_present

            if dex_cex_reverse_signal_present(snapshot):
                score += 6.0

        out[lane] = score

    return out


class AdaptiveLaneRouter:
    """Stateful router: caches fill rates for the process lifetime."""

    def __init__(self, backpack_client: Any | None = None) -> None:
        self.backpack = backpack_client
        self._cache_at: float = 0.0
        self._fill_rates: dict[str, float] = {lane: 0.0 for lane in LANES}
        self._cache_ttl = _env_float("ADAPTIVE_ROUTER_CACHE_SEC", 60.0)

    def refresh_fill_rates(self) -> dict[str, float]:
        now = time.time()
        if now - self._cache_at < self._cache_ttl:
            return self._fill_rates
        self._fill_rates = lane_fill_rates()
        self._cache_at = now
        return self._fill_rates

    async def adjust_scores(
        self,
        base_scores: dict[str, float],
        *,
        vol_5m_pct: float,
        snapshot: dict | None = None,
    ) -> dict[str, float]:
        rates = self.refresh_fill_rates()
        inv = await fetch_inventory_skew(self.backpack)
        adjusted = route_scores(
            base_scores,
            vol_5m_pct=vol_5m_pct,
            fill_rates=rates,
            inv_skew=float(inv.get("skew", 0.0)),
            snapshot=snapshot,
        )
        logger.debug(
            "AdaptiveRouter | fill_rates=%s skew=%.2f vol=%.3f%% scores=%s",
            {k: round(v, 3) for k, v in rates.items()},
            inv.get("skew", 0),
            vol_5m_pct,
            {k: round(v, 1) for k, v in adjusted.items()},
        )
        return adjusted
