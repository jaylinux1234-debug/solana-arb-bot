#!/usr/bin/env python3
"""Daily funnel dashboard — aggregate JSONL logs into fill-rate breakdowns."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

TRADE_LOG = Path("logs/trade_history.jsonl")
NEAR_MISS_LOG = Path("logs/cex_dex_near_misses.jsonl")
BRAIN_LOG = Path("logs/brain_choices.jsonl")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _pct(num: int, denom: int) -> str:
    if denom <= 0:
        return "n/a"
    return f"{100.0 * num / denom:.1f}%"


def _normalize_reason(raw: str) -> str:
    text = (raw or "unknown").strip()
    return text.split(":")[0] if ":" in text else text


def _trade_funnel(rows: list[dict]) -> dict:
    if not rows:
        return {"total": 0}

    fills = [
        r
        for r in rows
        if r.get("source") == "live_fill" or r.get("live_fill") is True
    ]
    blocked = [r for r in rows if r.get("source") == "live_blocked"]
    attempts = [r for r in rows if r.get("execution_attempt") or r.get("source") in (
        "live",
        "live_fill",
        "live_blocked",
    )]

    by_reason = Counter(_normalize_reason(str(r.get("block_reason") or r.get("reason") or "unknown")) for r in blocked)
    by_pair = Counter(str(r.get("pair") or r.get("symbol") or "?") for r in rows)
    fill_pairs = Counter(str(r.get("pair") or "?") for r in fills)

    return {
        "total_events": len(rows),
        "live_fills": len(fills),
        "live_blocked": len(blocked),
        "execution_attempts": len(attempts),
        "fill_rate": _pct(len(fills), len(fills) + len(blocked)),
        "top_block_reasons": dict(by_reason.most_common(12)),
        "events_by_pair": dict(by_pair.most_common(10)),
        "fills_by_pair": dict(fill_pairs.most_common(10)),
    }


def _near_miss_funnel(rows: list[dict]) -> dict:
    if not rows:
        return {"total": 0}

    by_reason = Counter(_normalize_reason(str(r.get("reason") or "unknown")) for r in rows)
    by_pair = Counter(str(r.get("pair") or "?") for r in rows)
    gross = [float(r["gross_bps"]) for r in rows if r.get("gross_bps") is not None]

    return {
        "total": len(rows),
        "top_reasons": dict(by_reason.most_common(12)),
        "by_pair": dict(by_pair.most_common(10)),
        "gross_bps_avg": round(sum(gross) / len(gross), 2) if gross else 0.0,
        "gross_bps_max": round(max(gross), 2) if gross else 0.0,
    }


def _brain_funnel(rows: list[dict]) -> dict:
    if not rows:
        return {"total": 0}

    picks = Counter(str(r.get("best_strategy") or r.get("picked") or "?") for r in rows)
    conf_sum: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        lane = str(r.get("best_strategy") or r.get("picked") or "?")
        try:
            conf_sum[lane].append(int(r.get("confidence") or 0))
        except (TypeError, ValueError):
            pass

    avg_conf = {
        lane: round(sum(vals) / len(vals), 1) if vals else 0.0
        for lane, vals in conf_sum.items()
    }

    return {
        "total_cycles": len(rows),
        "picks": dict(picks.most_common(8)),
        "avg_confidence_by_lane": avg_conf,
    }


def print_dashboard(*, tail: int | None = None) -> None:
    trades = _read_jsonl(TRADE_LOG)
    near = _read_jsonl(NEAR_MISS_LOG)
    brain = _read_jsonl(BRAIN_LOG)

    if tail:
        trades = trades[-tail:]
        near = near[-tail:]
        brain = brain[-tail:]

    trade_f = _trade_funnel(trades)
    near_f = _near_miss_funnel(near)
    brain_f = _brain_funnel(brain)

    print("=" * 64)
    print("CEX-DEX DAILY FUNNEL")
    print(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    if tail:
        print(f"Tail: last {tail} rows per log")
    print("=" * 64)

    print(f"\n## Trade funnel ({TRADE_LOG})")
    for key, val in trade_f.items():
        print(f"  {key}: {val}")

    print(f"\n## Near-miss funnel ({NEAR_MISS_LOG})")
    for key, val in near_f.items():
        print(f"  {key}: {val}")

    print(f"\n## Brain choices ({BRAIN_LOG})")
    for key, val in brain_f.items():
        print(f"  {key}: {val}")

    print("\n## Conversion summary")
    fills = int(trade_f.get("live_fills") or 0)
    blocked = int(trade_f.get("live_blocked") or 0)
    near_n = int(near_f.get("total") or 0)
    print(f"  live fills: {fills}")
    print(f"  live blocked: {blocked}")
    print(f"  near-misses (signals not traded): {near_n}")
    if fills + blocked > 0:
        print(f"  execution fill rate: {_pct(fills, fills + blocked)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily CEX-DEX funnel dashboard")
    parser.add_argument("--tail", type=int, default=None, help="Only last N rows per log")
    args = parser.parse_args()
    print_dashboard(tail=args.tail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
