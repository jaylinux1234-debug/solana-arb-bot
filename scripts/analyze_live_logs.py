#!/usr/bin/env python3
"""Quick live-log stats from docker + logs/*.jsonl."""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalize(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return re.sub(r"\r?\n\s+", " ", text)


def main() -> None:
    raw = subprocess.check_output(
        ["docker", "logs", "solana-arb-monitor", "--tail", "3000"],
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    flat = normalize(raw)

    scan = re.findall(
        r"CEX-DEX Scan \| edge_bps=([\d.-]+) spread_abs=([\d.-]+) net_bps=([\d.-]+) "
        r"dir=(\w+) confidence=([\d.]+) size_usdc=(\d+) probe=(\d+)",
        flat,
    )
    near = re.findall(
        r"NEAR_MISS \| gross=([\d.-]+) net=([\d.-]+) ai=([\d.]+)% reason=(\S+)",
        flat,
    )
    cycles = re.findall(
        r"Cycle #(\d+) \| PnL Today: \$([\d.]+) \| Loss Streak: (\d+) \| Can Trade: (\w+)",
        flat,
    )

    print("=== Docker (last 3000 lines) ===")
    print(f"scans={len(scan)} near_miss={len(near)} cycles_logged={len(cycles)}")
    if cycles:
        print(f"latest_cycle: #{cycles[-1][0]} pnl=${cycles[-1][1]} can_trade={cycles[-1][3]}")

    if scan:
        edges = [float(x[0]) for x in scan]
        dirs = Counter(x[3] for x in scan)
        print(f"scan_directions: {dict(dirs)}")
        print(
            f"edge_bps: min={min(edges):.1f} max={max(edges):.1f} avg={sum(edges)/len(edges):.2f}"
        )
        best = max(scan, key=lambda x: float(x[0]))
        print(
            f"best_scan: edge={best[0]} net={best[2]} conf={best[4]} size=${int(best[5])/1e6:.2f}"
        )

    if near:
        rc = Counter(x[3] for x in near)
        print(f"near_miss_reasons: {dict(rc.most_common())}")
        by: dict[str, list[float]] = {}
        for g, _n, _a, r in near:
            by.setdefault(r, []).append(float(g))
        for r, vals in sorted(by.items(), key=lambda kv: -len(kv[1])):
            print(
                f"  {r}: n={len(vals)} gross_avg={sum(vals)/len(vals):.2f} "
                f"min={min(vals):.1f} max={max(vals):.1f}"
            )

    p = ROOT / "logs" / "cex_dex_near_misses.jsonl"
    if p.exists():
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        recent = rows[-1000:]
        rc = Counter(r.get("reason", "?") for r in recent)
        gross = [float(r.get("gross_bps", 0)) for r in recent]
        print(f"\n=== near_miss jsonl (last {len(recent)} of {len(rows)} total) ===")
        print(f"reasons: {dict(rc.most_common(5))}")
        print(
            f"gross_bps: min={min(gross):.1f} max={max(gross):.1f} avg={sum(gross)/len(gross):.2f}"
        )
        cex_cheap = [g for r, g in zip(recent, gross) if "wrong" not in str(r.get("reason", ""))]
        if cex_cheap:
            above12 = sum(1 for g in cex_cheap if g >= 12)
            above6 = sum(1 for g in cex_cheap if g >= 6)
            print(
                f"tradeable_direction (non-wrong): n={len(cex_cheap)} "
                f"gross>=12: {above12} ({100*above12/len(cex_cheap):.1f}%) "
                f"gross>=6: {above6} ({100*above6/len(cex_cheap):.1f}%)"
            )


if __name__ == "__main__":
    main()
