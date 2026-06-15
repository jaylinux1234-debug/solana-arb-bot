#!/usr/bin/env python3
"""Daily measurement: live fills vs blocked attempts from trade_history.jsonl."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def analyze() -> None:
    p = Path("logs/trade_history.jsonl")
    if not p.exists():
        print("No trade history yet")
        return

    rows = [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    fills = sum(1 for r in rows if r.get("live_fill"))
    blocked = Counter(
        r.get("block_reason") for r in rows if r.get("source") == "live_blocked"
    )

    print(f"Real fills: {fills}")
    print(f"Blocked reasons: {blocked.most_common(5)}")
    print(f"Total attempts: {len(rows)}")


if __name__ == "__main__":
    analyze()
