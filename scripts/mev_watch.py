#!/usr/bin/env python3
"""MEV lane observability — summary stats + recent attempt tail from v2_attempts.jsonl."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

PATTERN = re.compile(
    r"backrun|collateral|liquidation|mev|kamino",
    re.IGNORECASE,
)

MEV_LANES = frozenset(
    {
        "backrun",
        "collateral_swap",
        "liquidation",
        "mev_idle",
        "dex_cex_reverse",
    }
)


def _load_attempts(path: Path) -> list[dict]:
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


def _is_success(row: dict) -> bool:
    if row.get("live_fill"):
        return True
    if row.get("executed") and row.get("block_reason") in (
        "success",
        row.get("lane"),
        "carry_found",
    ):
        return True
    if str(row.get("block_reason") or "") == "success":
        return True
    if str(row.get("status") or "") == "success":
        return True
    return False


def _lane_name(row: dict) -> str:
    event = str(row.get("event") or "")
    if event == "BACKRUN_ATTEMPT":
        return "backrun"
    if event == "COLLATERAL_SCAN":
        return "collateral_swap"
    if event in ("LIQUIDATION_SCAN", "LIQUIDATION_ATTEMPT"):
        return "liquidation"
    return str(row.get("lane") or row.get("execution_path") or "unknown")


def analyze_mev(attempts: list[dict], *, tail: int = 0) -> None:
    """Print MEV summary counters and success rates."""
    pool = attempts[-tail:] if tail > 0 else attempts
    mev_rows = [
        a
        for a in pool
        if _lane_name(a) in MEV_LANES
        or PATTERN.search(_lane_name(a))
        or PATTERN.search(str(a.get("event") or ""))
    ]
    if not mev_rows:
        mev_rows = pool

    by_lane = Counter(_lane_name(a) for a in mev_rows)
    success_by_lane: dict[str, float] = {}
    for lane in by_lane:
        lane_rows = [a for a in mev_rows if _lane_name(a) == lane]
        wins = sum(1 for a in lane_rows if _is_success(a))
        success_by_lane[lane] = wins / max(1, len(lane_rows))

    print("MEV Summary:", dict(by_lane))
    print("Success Rates:", {k: round(v, 3) for k, v in success_by_lane.items()})

    recent_profits = [
        a.get("profit_usd")
        for a in mev_rows[-10:]
        if a.get("profit_usd") not in (None, 0, 0.0)
    ]
    print("Recent profits:", recent_profits)

    reasons = Counter(str(a.get("block_reason") or a.get("event") or "?") for a in mev_rows)
    if reasons:
        print("Top block_reason/event:", reasons.most_common(8))


def tail_mev(path: Path, *, tail: int) -> int:
    """Legacy tail grep of MEV-related lines."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    recent = lines[-max(1, tail) :]
    hits = 0
    for line in recent:
        if not line.strip():
            continue
        if not PATTERN.search(line):
            continue
        hits += 1
        try:
            row = json.loads(line)
            ts = row.get("ts", "?")
            cycle = row.get("cycle", "?")
            lane = row.get("lane") or row.get("execution_path") or "?"
            reason = row.get("block_reason") or row.get("event") or "?"
            gross = row.get("gross_bps")
            net = row.get("net_bps")
            extra = ""
            if gross is not None:
                extra += f" | gross={float(gross):.1f}bps"
            if net is not None:
                extra += f" | net={float(net):.1f}bps"
            print(f"{ts} | cycle={cycle} | lane={lane} | reason={reason}{extra}")
        except json.JSONDecodeError:
            print(line[:200])
    print(f"\nMEV matches in last {len(recent)} lines: {hits}")
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="MEV activity watch on v2_attempts.jsonl")
    parser.add_argument(
        "--log",
        default=os.getenv("V2_ATTEMPTS_LOG", "logs/v2_attempts.jsonl"),
    )
    parser.add_argument("--tail", type=int, default=500, help="Recent lines to scan")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Only print aggregate MEV summary (no line tail)",
    )
    args = parser.parse_args()

    path = Path(args.log)
    if not path.is_file():
        print(f"No log file: {path}")
        return 1

    attempts = _load_attempts(path)
    analyze_mev(attempts, tail=args.tail if args.tail > 0 else 0)

    if not args.summary_only:
        print()
        tail_mev(path, tail=args.tail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
