#!/usr/bin/env python3
"""Summarize fill-rate optimization: trades, blocks, near-misses."""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

TRADE_LOG = Path("logs/trade_history.jsonl")
NEAR_MISS_LOG = Path("logs/cex_dex_near_misses.jsonl")
BASELINE = Path("logs/optimization_baseline.json")


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


def _trade_stats(rows: list[dict]) -> dict:
    if not rows:
        return {"total": 0}
    sources = Counter(str(r.get("source", "?")) for r in rows)
    fills = [r for r in rows if r.get("source") == "live_fill" or r.get("live_fill") is True]
    blocked = [r for r in rows if r.get("source") == "live_blocked"]
    reasons = Counter()
    pairs = Counter()
    for r in blocked:
        reason = str(r.get("block_reason") or r.get("reason") or "unknown")
        reasons[reason.split(":")[0]] += 1
        pairs[str(r.get("pair") or r.get("symbol") or "?")] += 1
    return {
        "total": len(rows),
        "live_fills": len(fills),
        "live_blocked": len(blocked),
        "sources": dict(sources.most_common(8)),
        "top_block_reasons": dict(reasons.most_common(10)),
        "top_blocked_pairs": dict(pairs.most_common(8)),
    }


def _near_miss_stats(rows: list[dict], *, tail: int | None = 500) -> dict:
    if not rows:
        return {"total": 0}
    sample = rows[-tail:] if tail and len(rows) > tail else rows
    dirs = Counter(str(r.get("direction", "?")) for r in sample)
    pairs = Counter(str(r.get("pair", "?")) for r in sample)
    return {
        "total": len(rows),
        "sampled": len(sample),
        "directions": dict(dirs.most_common(5)),
        "pairs": dict(pairs.most_common(8)),
    }


def main() -> int:
    trades = _read_jsonl(TRADE_LOG)
    near = _read_jsonl(NEAR_MISS_LOG)
    trade_s = _trade_stats(trades)
    near_s = _near_miss_stats(near)

    print("=" * 60)
    print("FILL-RATE OPTIMIZATION SUMMARY")
    print(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    print("\n## Trade history", TRADE_LOG)
    for k, v in trade_s.items():
        print(f"  {k}: {v}")

    print("\n## Near-misses", NEAR_MISS_LOG)
    for k, v in near_s.items():
        print(f"  {k}: {v}")

    if BASELINE.is_file():
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        print("\n## Baseline (pre-optimization snapshot)")
        print(json.dumps(baseline, indent=2))
        b_tr = baseline.get("trade_stats", {})
        if b_tr.get("total"):
            delta_blocked = trade_s.get("live_blocked", 0) - b_tr.get("live_blocked", 0)
            delta_fills = trade_s.get("live_fills", 0) - b_tr.get("live_fills", 0)
            print(f"\n## Delta since baseline")
            print(f"  live_fills: {delta_fills:+d}")
            print(f"  live_blocked: {delta_blocked:+d}")
    else:
        snap = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "trade_stats": trade_s,
            "near_miss_stats": near_s,
        }
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        print(f"\nWrote baseline snapshot → {BASELINE}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
