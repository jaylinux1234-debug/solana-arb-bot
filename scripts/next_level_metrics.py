#!/usr/bin/env python3
"""Next-level KPIs from ``logs/trade_history.jsonl`` (fills, blocks, Sharpe)."""

from __future__ import annotations

import json
import os
import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TRADE_LOG = Path(os.getenv("TRADE_HISTORY_PATH", "logs/trade_history.jsonl"))
WINDOW_HOURS = float(os.getenv("NEXT_LEVEL_METRICS_WINDOW_HOURS", "24"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def row_timestamp(row: dict[str, Any]) -> datetime | None:
    """Parse ``timestamp`` (ISO) or ``ts`` (unix seconds)."""
    raw = row.get("timestamp")
    if raw:
        text = str(raw).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
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


def is_live_fill(row: dict[str, Any]) -> bool:
    if row.get("live_fill") is True:
        return True
    return str(row.get("source") or "").lower() == "live_fill"


def load_trades(log_path: Path | str = TRADE_LOG) -> list[dict[str, Any]]:
    return _read_jsonl(Path(log_path))


def compute_time_to_first_fill(
    log_path: Path | str = TRADE_LOG,
    *,
    window_hours: float = WINDOW_HOURS,
) -> str | float:
    """
    Hours from the first log event in the lookback window to the first ``live_fill``.

    Returns ``"No fills yet"`` when no fill exists (caller can read ``hours_since_window_start``).
    """
    rows = load_trades(log_path)
    if not rows:
        return "No fills yet (empty log)"

    now = datetime.now(UTC)
    window_start = now - timedelta(hours=window_hours)

    timed: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        dt = row_timestamp(row)
        if dt is not None:
            timed.append((dt, row))

    if not timed:
        return "No fills yet (no timestamps in log)"

    timed.sort(key=lambda x: x[0])
    in_window = [(t, r) for t, r in timed if t >= window_start]
    series = in_window if in_window else timed
    start_at = series[0][0]

    fill_times = [t for t, r in series if is_live_fill(r)]
    if not fill_times:
        elapsed_h = (now - start_at).total_seconds() / 3600.0
        return f"No fills yet ({elapsed_h:.1f}h since window start, {len(series)} attempts)"

    first_fill = min(fill_times)
    return round((first_fill - start_at).total_seconds() / 3600.0, 2)


def risk_adjusted_sharpe(trades: list[dict[str, Any]]) -> float:
    """
    Mean / std of per-trade PnL (``realized_usdc`` or ``pnl_usd``).

    Uses pandas when installed; otherwise stdlib ``statistics``.
    """
    pnls: list[float] = []
    for row in trades:
        if not is_live_fill(row):
            continue
        pnl = row.get("pnl_usd")
        if pnl is None:
            pnl = row.get("realized_usdc")
        if pnl is None:
            continue
        try:
            pnls.append(float(pnl))
        except (TypeError, ValueError):
            continue

    if len(pnls) < 2:
        return 0.0

    try:
        import pandas as pd

        returns = pd.Series(pnls).pct_change().dropna()
        if len(returns) < 2 or returns.std() == 0:
            return 0.0
        return float(returns.mean() / returns.std())
    except ImportError:
        stdev = statistics.pstdev(pnls)
        if stdev == 0:
            return 0.0
        return statistics.mean(pnls) / stdev


def summarize(log_path: Path | str = TRADE_LOG) -> dict[str, Any]:
    """Aggregate next-level metrics for printing or dashboards."""
    rows = load_trades(log_path)
    fills = [r for r in rows if is_live_fill(r)]
    blocked = [r for r in rows if str(r.get("source") or "") == "live_blocked"]
    block_reasons = Counter(
        str(r.get("block_reason") or "unknown").split(":")[0] for r in blocked
    )
    total_realized = sum(float(r.get("realized_usdc") or 0) for r in fills)

    return {
        "generated_utc": datetime.now(UTC).isoformat(),
        "log_path": str(log_path),
        "total_rows": len(rows),
        "live_fills": len(fills),
        "live_blocked": len(blocked),
        "fill_rate_pct": round(100.0 * len(fills) / len(rows), 2) if rows else 0.0,
        "hours_to_first_fill": compute_time_to_first_fill(log_path),
        "risk_adjusted_sharpe": round(risk_adjusted_sharpe(rows), 4),
        "total_realized_usdc": round(total_realized, 4),
        "top_block_reasons": dict(block_reasons.most_common(5)),
    }


def main() -> int:
    stats = summarize()
    print("=" * 60)
    print("NEXT-LEVEL METRICS")
    print(f"Generated: {stats['generated_utc']}")
    print(f"Log: {stats['log_path']}")
    print("=" * 60)
    print(f"Hours to first fill: {stats['hours_to_first_fill']}")
    print(f"Live fills: {stats['live_fills']} / {stats['total_rows']} ({stats['fill_rate_pct']}%)")
    print(f"Live blocked: {stats['live_blocked']}")
    print(f"Total realized USDC: {stats['total_realized_usdc']}")
    print(f"Risk-adjusted Sharpe (fills): {stats['risk_adjusted_sharpe']}")
    if stats["top_block_reasons"]:
        print(f"Top block reasons: {stats['top_block_reasons']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
