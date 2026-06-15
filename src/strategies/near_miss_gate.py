"""Filter and rate-limit CEX-DEX near-miss logging."""

from __future__ import annotations

import os
import time
from collections import defaultdict

_last_near_miss_log: dict[str, float] = defaultdict(float)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def near_miss_log_enabled() -> bool:
    return _env_bool("CEX_DEX_LOG_NEAR_MISSES", True)


def should_emit_near_miss(
    reason: str,
    *,
    gross_bps: float = 0.0,
    net_bps: float = 0.0,
    min_net_bps: float | None = None,
    direction: str = "",
) -> bool:
    """
    Return True when a near-miss should be written to logs / JSONL.

    Suppresses expected dex-cheap idle, deep unprofitable nets, weak gross, and
    per-reason cooldown bursts.
    """
    if not near_miss_log_enabled():
        return False

    reason_key = (reason or "unknown").split("|")[0].strip().lower()
    direction_l = (direction or "").strip().lower()

    if _env_bool("ENABLE_DEX_CEX_REVERSE", True) and (
        reason_key in ("wrong_direction_dex_cheap", "dex_cheap", "dex_cex_reverse_idle")
        or direction_l == "dex_cheap"
        or "wrong_direction_dex_cheap" in reason.lower()
    ):
        wrong_min = _env_float("CEX_DEX_NEAR_MISS_WRONG_DIR_MIN_GROSS_BPS", 12.0)
        if gross_bps < wrong_min:
            return False

    if min_net_bps is not None:
        net_band = _env_float("CEX_DEX_NEAR_MISS_NET_BAND_BPS", 4.0)
        if net_bps < float(min_net_bps) - net_band:
            return False

    min_gross = _env_float("CEX_DEX_NEAR_MISS_MIN_GROSS_BPS", 6.0)
    if gross_bps < min_gross:
        return False

    cooldown = _env_float("NEAR_MISS_LOG_COOLDOWN_SEC", 45.0)
    now = time.time()
    if now - _last_near_miss_log[reason_key] < cooldown:
        return False

    _last_near_miss_log[reason_key] = now
    return True
