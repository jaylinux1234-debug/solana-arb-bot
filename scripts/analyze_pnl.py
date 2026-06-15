#!/usr/bin/env python3
"""Summarize true net P&L from logs/v2_pnl.jsonl."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    log_path = Path(os.getenv("V2_PNL_LOG", "logs/v2_pnl.jsonl"))
    if not log_path.is_file():
        print(f"No P&L log at {log_path} — waiting for first live fill.")
        return 0

    rows: list[dict] = []
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    if not rows:
        print(f"{log_path} is empty — no fills recorded yet.")
        return 0

    try:
        import pandas as pd

        df = pd.DataFrame(rows)
        print(df.describe(include="all"))
        print(f"Total net profit: ${df['net_usdc'].sum():.4f}")
        out_csv = Path("logs/pnl_summary.csv")
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_csv, index=False)
        print(f"Wrote {out_csv}")
    except ImportError:
        net_total = sum(float(r.get("net_usdc") or 0) for r in rows)
        print(f"Fills: {len(rows)}")
        print(f"Total net profit: ${net_total:.4f}")
        for r in rows:
            print(
                f"  {r.get('ts')} net=${float(r.get('net_usdc') or 0):.4f} "
                f"trade=${float(r.get('trade_usdc') or 0):.2f} tx={str(r.get('tx_sig') or '')[:16]}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
