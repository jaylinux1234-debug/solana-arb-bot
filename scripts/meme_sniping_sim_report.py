#!/usr/bin/env python3
"""Parse meme sniping sim logs into a readable report."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from pathlib import Path


def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True, errors="replace")


def parse_docker_logs(container: str) -> dict:
    raw = _run(["docker", "logs", container, "2>&1"])
    lines = raw.splitlines()

    scans = [ln for ln in lines if "meme_sniping_scan" in ln]
    filters = [ln for ln in lines if "meme_sniping_filter" in ln]
    signals = [ln for ln in lines if "meme_sniping_strong_signal" in ln]
    sim_entries = [ln for ln in lines if "[SIM] meme_snipe v2" in ln]
    sim_sells = [ln for ln in lines if "[SIM] meme_snipe sell" in ln]
    summaries = [ln for ln in lines if "meme_sniping_sim_summary" in ln]
    pump_warn = [ln for ln in lines if "pump.fun API unavailable" in ln]

    reject_reasons = Counter()
    for ln in filters:
        if "approved=False" in ln or "approved=false" in ln:
            if "vol_below_min" in ln:
                reject_reasons["vol_below_min"] += 1
            elif "social_below_min" in ln:
                reject_reasons["social_below_min"] += 1
            else:
                reject_reasons["ai_rejected"] += 1

    exit_reasons = Counter()
    for ln in sim_sells:
        m = re.search(r"reason=([^|]+)", ln)
        if m:
            exit_reasons[m.group(1).strip()] += 1

    return {
        "scan_logs": len(scans),
        "filter_reviews": len(filters),
        "strong_signals": len(signals),
        "sim_entries": len(sim_entries),
        "sim_exits": len(sim_sells),
        "periodic_summaries": len(summaries),
        "pump_fallback_warnings": len(pump_warn),
        "reject_reasons": dict(reject_reasons),
        "exit_reasons": dict(exit_reasons),
        "last_scan": scans[-1] if scans else None,
        "last_summary": summaries[-1] if summaries else None,
        "recent_sim_trades": (sim_entries + sim_sells)[-20:],
    }


def parse_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Meme sniping sim report")
    parser.add_argument("--container", default="solana-arb-monitor")
    parser.add_argument("--jsonl", default="logs/meme_sniping_sim.jsonl")
    args = parser.parse_args()

    report = parse_docker_logs(args.container)
    report["jsonl_snapshots"] = parse_jsonl(Path(args.jsonl))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
