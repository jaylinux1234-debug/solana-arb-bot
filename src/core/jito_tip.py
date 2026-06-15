"""Jito dynamic tip floor API + profit-proportional tip sizing (small accounts)."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_JITO_OUTCOMES: deque[tuple[float, bool]] = deque(maxlen=64)

JITO_TIP_FLOOR_URL = os.getenv(
    "JITO_TIP_FLOOR_URL",
    "https://bundles-api-rest.jito.wtf/api/v1/bundles/tip_floor",
).strip()

_FLOOR_CACHE: dict[str, Any] | None = None
_FLOOR_CACHE_AT: float = 0.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _tip_bound(name: str, legacy: str, default: int) -> int:
    raw = (os.getenv(name) or os.getenv(legacy) or str(default)).strip()
    return int(raw)


def _floor_cache_ttl_sec() -> float:
    return max(5.0, _env_float("JITO_TIP_FLOOR_CACHE_SEC", 45.0))


def _fallback_tip_floor() -> dict[str, int]:
    return {
        "min": _tip_bound("JITO_TIP_MIN_LAMPORTS", "JITO_TIP_LAMPORTS_MIN", 25_000),
        "median": int(os.getenv("JITO_TIP_FLOOR_FALLBACK_MEDIAN", "60000")),
        "max": _tip_bound("JITO_TIP_MAX_LAMPORTS", "JITO_TIP_LAMPORTS_MAX", 150_000),
    }


def _parse_tip_floor_payload(data: Any) -> dict[str, int]:
    """Normalize Jito tip_floor JSON (flat or nested percentiles)."""
    if not isinstance(data, dict):
        return _fallback_tip_floor()

    if "median" in data:
        return {
            "min": int(data.get("min", 1000)),
            "median": int(data.get("median", 50_000)),
            "max": int(data.get("max", 200_000)),
        }

    # Some responses nest by time window, e.g. landed_tips_50th_percentile
    for key in ("landed_tips_50th_percentile", "ema_landed_tips_50th_percentile"):
        if key in data:
            med = int(data[key])
            return {
                "min": int(data.get("landed_tips_25th_percentile", med // 2)),
                "median": med,
                "max": int(data.get("landed_tips_75th_percentile", med * 2)),
            }

    return _fallback_tip_floor()


async def get_current_tip_floor(*, force_refresh: bool = False) -> dict[str, int]:
    """Live tip percentiles from Jito (cached ~45s by default)."""
    global _FLOOR_CACHE, _FLOOR_CACHE_AT

    now = time.monotonic()
    if (
        not force_refresh
        and _FLOOR_CACHE is not None
        and (now - _FLOOR_CACHE_AT) < _floor_cache_ttl_sec()
    ):
        return dict(_FLOOR_CACHE)

    timeout = _env_float("JITO_TIP_FLOOR_TIMEOUT_SEC", 3.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(JITO_TIP_FLOOR_URL)
            resp.raise_for_status()
            floor = _parse_tip_floor_payload(resp.json())
    except Exception as exc:
        logger.debug("Jito tip floor fetch failed (%s); using fallback", exc)
        floor = _fallback_tip_floor()

    _FLOOR_CACHE = floor
    _FLOOR_CACHE_AT = now
    return dict(floor)


def get_cached_tip_floor() -> dict[str, int]:
    """Sync accessor — cached value or static fallback (no network)."""
    if _FLOOR_CACHE is not None:
        return dict(_FLOOR_CACHE)
    return _fallback_tip_floor()


def expected_profit_lamports(net_bps: float, size_usdc_micro: int) -> int:
    """Convert modeled net bps + trade size to expected profit in SOL lamports."""
    net_usd = modeled_net_usd(net_bps, size_usdc_micro)
    if net_usd <= 0:
        return 0
    sol_usd = max(_env_float("JITO_TIP_SOL_USD", 180.0), 1.0)
    return int(net_usd / sol_usd * 1_000_000_000)


def compute_dynamic_jito_tip_lamports(
    expected_profit_lamports: int,
    *,
    fill_rate_mult: float = 1.0,
    confidence: float | None = None,
) -> int:
    """
    Profit-ratio tip sizing (sync core).

    ``max(base_tip, min(profit * ratio * fill_mult, cap))`` — no static env floor.
    """
    base_tip = _tip_bound("JITO_TIP_MIN_LAMPORTS", "JITO_TIP_LAMPORTS_MIN", 80_000)
    max_tip = _tip_bound("JITO_TIP_MAX_LAMPORTS", "JITO_TIP_LAMPORTS_MAX", 250_000)
    profit_ratio = _env_float("JITO_TIP_PROFIT_RATIO", 0.35)
    if confidence is not None:
        conf = max(50.0, min(100.0, float(confidence)))
        profit_ratio *= 0.85 + 0.15 * (conf / 100.0)

    profit_lam = max(0, int(expected_profit_lamports))
    dynamic_tip = int(profit_lam * profit_ratio * max(0.5, float(fill_rate_mult)))
    return max(base_tip, min(dynamic_tip, max_tip))


def record_jito_bundle_outcome(landed: bool) -> None:
    """Track recent bundle landing outcomes for fill-rate-aware tipping."""
    _JITO_OUTCOMES.append((time.monotonic(), bool(landed)))


def _jito_fill_rate_boost(fill_rate: float) -> float:
    """Raise tip when landing rate is low; trim slightly when consistently high."""
    target = _env_float("JITO_TIP_FILL_RATE_TARGET", 0.6)
    rate = max(0.0, min(1.0, float(fill_rate)))
    if rate < target and target > 0:
        deficit = (target - rate) / target
        max_boost = _env_float("JITO_TIP_FILL_RATE_MAX_BOOST", 0.30)
        return 1.0 + deficit * max_boost
    if rate >= _env_float("JITO_TIP_HIGH_FILL_RATE", 0.85):
        return _env_float("JITO_TIP_HIGH_FILL_DISCOUNT", 0.92)
    return 1.0


def _row_ts(row: dict[str, Any]) -> datetime | None:
    raw = row.get("ts") or row.get("timestamp")
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


def jito_recent_bundle_fill_rate(
    *,
    hours: float | None = None,
    log_path: Path | None = None,
) -> float:
    """Recent Jito bundle fill rate from in-memory outcomes or v2 attempt logs."""
    window_sec = max(300.0, (hours or _env_float("JITO_TIP_FILL_RATE_WINDOW_HOURS", 2.0)) * 3600.0)
    cutoff = time.monotonic() - window_sec
    recent = [landed for ts, landed in _JITO_OUTCOMES if ts >= cutoff]
    if len(recent) >= 3:
        return sum(recent) / len(recent)

    path = log_path or Path(
        os.getenv("V2_ATTEMPTS_LOG", "logs/v2_attempts.jsonl").strip()
        or "logs/v2_attempts.jsonl"
    )
    if not path.is_file():
        return 1.0

    dt_cutoff = datetime.now(UTC) - timedelta(seconds=window_sec)
    attempts = 0
    fills = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("send_path") or "") != "jito":
            continue
        dt = _row_ts(row)
        if dt is not None and dt < dt_cutoff:
            continue
        if row.get("executed") or row.get("event") == "EXECUTING_REVERSE_ARB":
            attempts += 1
            if row.get("live_fill") is True:
                fills += 1
    if attempts >= 2:
        return fills / attempts
    return 1.0


async def get_dynamic_jito_tip(
    expected_profit_lamports: int,
    *,
    confidence: float | None = None,
) -> int:
    """
    Dynamic Jito tip from expected profit + recent fill rate + live tip floor.

    Replaces static ``JITO_TIP_LAMPORTS`` for execution paths.
    """
    fill_rate = jito_recent_bundle_fill_rate()
    fill_mult = _jito_fill_rate_boost(fill_rate)
    tip = compute_dynamic_jito_tip_lamports(
        expected_profit_lamports,
        fill_rate_mult=fill_mult,
        confidence=confidence,
    )

    if _env_bool("JITO_TIP_USE_LIVE_FLOOR", True):
        floor = await get_current_tip_floor()
        floor_mult = _env_float("JITO_TIP_FLOOR_MULTIPLIER", 1.05)
        live_floor = int(floor.get("median", tip) * floor_mult)
        max_tip = _tip_bound("JITO_TIP_MAX_LAMPORTS", "JITO_TIP_LAMPORTS_MAX", 250_000)
        tip = max(tip, live_floor)
        tip = min(tip, max_tip)

    sol_usd = max(_env_float("JITO_TIP_SOL_USD", 180.0), 1.0)
    tip_usd = (tip / 1_000_000_000.0) * sol_usd
    profit_usd = (max(0, int(expected_profit_lamports)) / 1_000_000_000.0) * sol_usd
    pct = (tip_usd / profit_usd * 100.0) if profit_usd > 0 else 0.0
    logger.info(
        "Dynamic Jito tip | lamports=%s fill_rate=%.2f mult=%.2f (%.1f%% of profit)",
        tip,
        fill_rate,
        fill_mult,
        pct,
    )
    return tip


async def resolve_v2_execution_jito_tip(
    net_bps: float,
    size_usdc_micro: int,
    *,
    gross_bps: float | None = None,
    strong_signal: bool = False,
    confidence: float | None = None,
) -> int:
    """v2 execution path: profit-proportional tip with optional STRONG boost."""
    profit_lam = expected_profit_lamports(net_bps, size_usdc_micro)
    if profit_lam <= 0 and gross_bps is not None:
        profit_lam = expected_profit_lamports(float(gross_bps) * 0.15, size_usdc_micro)

    conf = confidence if confidence is not None else (0.95 if strong_signal else None)
    tip = await get_dynamic_jito_tip(profit_lam, confidence=conf)

    if strong_signal:
        mult = _env_float("V2_STRONG_JITO_TIP_MULT", 1.15)
        tip = int(tip * mult)
        max_tip = _tip_bound("JITO_TIP_MAX_LAMPORTS", "JITO_TIP_LAMPORTS_MAX", 250_000)
        tip = min(tip, max_tip)

    return tip


def calculate_optimal_tip(
    modeled_net_usd: float,
    gross_bps: float = 0.0,
    *,
    confidence: float | None = None,
    tip_floor: dict[str, int] | None = None,
) -> int:
    """
    Profit-proportional tipping optimized for small accounts.

    ``modeled_net_usd`` — expected net profit in USDC for the trade.
    ``gross_bps`` — optional spread context.
    ``confidence`` — optional AI confidence (50–100); lowers tip when uncertain.
    """
    sol_usd = max(_env_float("JITO_TIP_SOL_USD", 180.0), 1.0)
    profit_lam = (
        int(modeled_net_usd / sol_usd * 1_000_000_000) if modeled_net_usd > 0 else 0
    )
    fill_mult = _jito_fill_rate_boost(jito_recent_bundle_fill_rate())
    tip = compute_dynamic_jito_tip_lamports(
        profit_lam,
        fill_rate_mult=fill_mult,
        confidence=confidence,
    )

    floor_data = tip_floor if tip_floor is not None else get_cached_tip_floor()
    floor_mult = _env_float("JITO_TIP_FLOOR_MULTIPLIER", 1.05)
    live_floor = int(floor_data.get("median", tip) * floor_mult)
    max_lamports = _tip_bound("JITO_TIP_MAX_LAMPORTS", "JITO_TIP_LAMPORTS_MAX", 250_000)
    min_lamports = _tip_bound("JITO_TIP_MIN_LAMPORTS", "JITO_TIP_LAMPORTS_MIN", 80_000)

    optimal = max(tip, live_floor, min_lamports)
    return min(optimal, max_lamports)


async def calculate_optimal_tip_async(
    modeled_net_usd: float,
    gross_bps: float = 0.0,
) -> int:
    """Async variant with live tip-floor fetch."""
    floor = await get_current_tip_floor()
    return calculate_optimal_tip(modeled_net_usd, gross_bps, tip_floor=floor)


def calculate_optimal_tip_from_bps(
    net_bps: float,
    size_usdc_micro: int,
    *,
    gross_bps: float = 0.0,
    tip_floor: dict[str, int] | None = None,
) -> int:
    """Convert bps + trade size to USD net, then delegate to ``calculate_optimal_tip``."""
    modeled_net_usd = (float(net_bps) / 10_000.0) * (int(size_usdc_micro) / 1_000_000.0)
    return calculate_optimal_tip(modeled_net_usd, gross_bps=gross_bps, tip_floor=tip_floor)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def mev_protection_enabled() -> bool:
    return _env_bool("MEV_PROTECTION_ENABLED", True)


def modeled_net_usd(net_bps: float, size_usdc_micro: int) -> float:
    return (float(net_bps) / 10_000.0) * (int(size_usdc_micro) / 1_000_000.0)


def log_jito_tip(tip_lamports: int, expected_net_usd: float) -> None:
    sol_usd = max(_env_float("JITO_TIP_SOL_USD", 180.0), 1.0)
    tip_usd = (tip_lamports / 1_000_000_000.0) * sol_usd
    pct = (tip_usd / expected_net_usd * 100.0) if expected_net_usd > 0 else 0.0
    logger.info(
        "Jito tip: %.5f SOL (%.1f%% of profit)",
        tip_lamports / 1_000_000_000.0,
        pct,
    )


def resolve_jito_tip_for_opportunity(
    *,
    net_bps: float,
    gross_bps: float,
    size_usdc_micro: int,
    override_net_usd: float | None = None,
    confidence: float | None = None,
    log: bool = True,
) -> int:
    """Compute tip from opportunity fields (used by Jupiter + CEX-DEX execute paths)."""
    if not mev_protection_enabled():
        return int(os.getenv("JITO_TIP_LAMPORTS", "100000"))

    expected_net_usd = (
        float(override_net_usd)
        if override_net_usd is not None
        else modeled_net_usd(net_bps, size_usdc_micro)
    )
    if expected_net_usd <= 0:
        expected_net_usd = _env_float("JITO_TIP_FALLBACK_NET_USD", 8.0)

    tip = calculate_optimal_tip(
        expected_net_usd,
        gross_bps=float(gross_bps),
        confidence=confidence,
        tip_floor=get_cached_tip_floor(),
    )
    if log:
        log_jito_tip(tip, expected_net_usd)
    return tip


def compute_tip_from_profit_usdc(profit_usdc: float) -> int:
    """
    Profit-tier tip sizing with fill-rate target (sync helper for bundle send).
    """
    base_tip = int(os.getenv("JITO_TIP_LAMPORTS", "120000"))
    fill_target = _env_float("JITO_TIP_FILL_RATE_TARGET", 0.6)

    dynamic_mult = 1.0
    if profit_usdc > 8.0:
        dynamic_mult = _env_float("V2_STRONG_JITO_TIP_MULT", 1.15)
    elif profit_usdc < 3.0:
        dynamic_mult = 0.85

    tip_lamports = int(base_tip * dynamic_mult * (1 + (1 - fill_target) * 0.4))
    min_tip = _tip_bound("JITO_TIP_MIN_LAMPORTS", "JITO_TIP_LAMPORTS_MIN", 50_000)
    max_tip = _tip_bound("JITO_TIP_MAX_LAMPORTS", "JITO_TIP_LAMPORTS_MAX", 300_000)
    return max(min_tip, min(tip_lamports, max_tip))


def resolve_jito_tip_for_trade(
    net_bps: float,
    gross_bps: float,
    size_usdc_micro: int,
    *,
    confidence: float | None = None,
    log: bool = True,
) -> int:
    """MEV-aware optimal tip from modeled net + gross spread."""
    return resolve_jito_tip_for_opportunity(
        net_bps=net_bps,
        gross_bps=gross_bps,
        size_usdc_micro=size_usdc_micro,
        confidence=confidence,
        log=log,
    )
