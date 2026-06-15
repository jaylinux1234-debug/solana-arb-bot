"""Persist CEX-DEX near-misses for daily AI strategy review."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NEAR_MISS_PATH = Path(
    os.getenv("CEX_DEX_NEAR_MISS_LOG_PATH", "logs/cex_dex_near_misses.jsonl")
)


def append_cex_dex_near_miss(record: dict[str, Any]) -> None:
    """Append one near-miss row (JSONL) when ``CEX_DEX_LOG_NEAR_MISSES`` is enabled."""
    if not _log_enabled():
        return
    NEAR_MISS_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        **record,
        "timestamp": datetime.now(UTC).isoformat(),
        "kind": "cex_dex_near_miss",
    }
    with NEAR_MISS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")


def load_near_miss_summary_for_daily_review(*, max_rows: int = 80) -> dict[str, Any]:
    """
    Summarize recent near-misses for ``maybe_daily_strategy_improvement`` / daily AI review.
    """
    if not NEAR_MISS_PATH.is_file():
        return {"count": 0, "samples": []}

    lines = NEAR_MISS_PATH.read_text(encoding="utf-8").splitlines()
    rows: list[dict[str, Any]] = []
    for line in lines[-max_rows:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not rows:
        return {"count": 0, "samples": []}

    gross_vals = [
        float(r["gross_bps"])
        for r in rows
        if r.get("gross_bps") is not None
    ]
    reasons: dict[str, int] = {}
    for r in rows:
        reason = str(r.get("reason") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1

    hourly = hourly_slippage_patterns(rows)

    return {
        "count": len(rows),
        "gross_bps_avg": sum(gross_vals) / len(gross_vals) if gross_vals else 0.0,
        "gross_bps_max": max(gross_vals) if gross_vals else 0.0,
        "reason_counts": reasons,
        "hourly_patterns": hourly,
        "samples": rows[-12:],
    }


def hourly_slippage_patterns(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Aggregate near-misses by UTC hour for daily AI review (slippage / gate friction).
    """
    by_hour: dict[int, list[float]] = {}
    net_by_hour: dict[int, list[float]] = {}

    for row in rows:
        ts_raw = row.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        hour = ts.hour
        gross = row.get("gross_bps")
        net = row.get("net_bps")
        if gross is not None:
            by_hour.setdefault(hour, []).append(float(gross))
        if net is not None:
            net_by_hour.setdefault(hour, []).append(float(net))

    worst_hours: list[dict[str, Any]] = []
    for hour in sorted(set(by_hour) | set(net_by_hour)):
        gross_avg = (
            sum(by_hour.get(hour, [])) / len(by_hour[hour])
            if by_hour.get(hour)
            else None
        )
        net_avg = (
            sum(net_by_hour.get(hour, [])) / len(net_by_hour[hour])
            if net_by_hour.get(hour)
            else None
        )
        worst_hours.append(
            {
                "hour_utc": hour,
                "count": max(len(by_hour.get(hour, [])), len(net_by_hour.get(hour, []))),
                "gross_bps_avg": round(gross_avg, 2) if gross_avg is not None else None,
                "net_bps_avg": round(net_avg, 2) if net_avg is not None else None,
            }
        )

    worst_hours.sort(
        key=lambda x: (x.get("net_bps_avg") is not None, x.get("net_bps_avg") or 0.0)
    )
    high_slippage_hours = [
        h for h in worst_hours if h.get("net_bps_avg") is not None and h["net_bps_avg"] < 4
    ][:6]

    return {
        "by_hour": worst_hours,
        "likely_high_slippage_hours_utc": high_slippage_hours,
        "note": "Hours with low avg net_bps on near-misses often correlate with wider Jupiter/CEX drag.",
    }


def _log_enabled() -> bool:
    raw = (os.getenv("CEX_DEX_LOG_NEAR_MISSES") or "true").strip().lower()
    return raw in ("1", "true", "yes", "on")
