#!/usr/bin/env python3
"""Offline replay: estimate uplift from exec-size ladder using recent CEX-DEX scan logs.

Method:
- Parse lines like:
  CEX-DEX Scan | pair=... probe_edge=.. exec_edge=.. cost_bps=.. net_bps=.. size_usdc=.. probe=..
- Recompute modeled cost at ladder sizes.
- Keep exec_edge fixed per sample (conservative assumption).
- Report net uplift potential and gate-cross counts.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path

SCAN_RE_NEW = re.compile(
    r"CEX-DEX Scan \| pair=(?P<pair>[^\s]+)\s+"
    r"probe_edge=(?P<probe_edge>-?\d+(?:\.\d+)?)\s+"
    r"exec_edge=(?P<exec_edge>-?\d+(?:\.\d+)?)\s+"
    r"spread_abs=(?P<spread_abs>-?\d+(?:\.\d+)?)\s+"
    r"cost_bps=(?P<cost_bps>-?\d+(?:\.\d+)?)\s+"
    r"net_bps=(?P<net_bps>-?\d+(?:\.\d+)?)\s+"
    r"dir=(?P<direction>\w+)\s+"
    r"confidence=(?P<confidence>-?\d+(?:\.\d+)?)\s+"
    r"size_usdc=(?P<size_usdc>\d+)\s+"
    r"probe=(?P<probe>\d+)"
)

SCAN_RE_OLD = re.compile(
    r"CEX-DEX Scan \| edge_bps=(?P<edge>-?\d+(?:\.\d+)?)\s+"
    r"spread_abs=(?P<spread_abs>-?\d+(?:\.\d+)?)\s+"
    r"net_bps=(?P<net_bps>-?\d+(?:\.\d+)?)\s+"
    r"dir=(?P<direction>\w+)\s+"
    r"confidence=(?P<confidence>-?\d+(?:\.\d+)?)\s+"
    r"size_usdc=(?P<size_usdc>\d+)\s+"
    r"probe=(?P<probe>\d+)"
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class ScanRow:
    pair: str
    probe_edge: float
    exec_edge: float
    cost_bps: float
    net_bps: float
    size_usdc_micro: int


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _modeled_cost(size_usdc_micro: int) -> float:
    # Keep this aligned with src/strategies/cex_dex_core.modeled_roundtrip_cost_bps
    use_component = str(os.getenv("CEX_DEX_USE_COMPONENT_COST_MODEL", "true")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if use_component:
        base = (
            _env_float("CEX_DEX_CEX_FEE_ROUNDTRIP_BPS", 18.0)
            + _env_float("CEX_DEX_JUPITER_LEG_FEE_BUFFER_BPS", 12.0)
            + _env_float("CEX_DEX_EXECUTION_SLIPPAGE_BUFFER_BPS", 28.0)
            + _env_float("CEX_DEX_WITHDRAWAL_LATENCY_BPS", 0.0)
        )
    else:
        base = _env_float("CEX_DEX_STRATEGY_BASE_COST_BPS", 9.0)

    size_usdc = max(0.0, size_usdc_micro / 1_000_000.0)
    max_impact = _env_float("CEX_DEX_MAX_SIZE_IMPACT_BPS", 3.0)
    impact_slope = _env_float("CEX_DEX_SIZE_IMPACT_SLOPE_BPS", 4.0)
    ref_usdc = max(1.0, _env_float("CEX_DEX_SIZE_IMPACT_REF_USDC", 30.0))
    impact_exp = _env_float("CEX_DEX_SIZE_IMPACT_EXPONENT", 1.12)
    impact = min(max_impact, impact_slope * ((size_usdc / ref_usdc) ** impact_exp))
    return base + impact


def _load_rows(path: Path, max_rows: int) -> list[ScanRow]:
    rows: list[ScanRow] = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = ANSI_RE.sub("", raw)
        m_new = SCAN_RE_NEW.search(line)
        if m_new:
            rows.append(
                ScanRow(
                    pair=m_new.group("pair"),
                    probe_edge=float(m_new.group("probe_edge")),
                    exec_edge=float(m_new.group("exec_edge")),
                    cost_bps=float(m_new.group("cost_bps")),
                    net_bps=float(m_new.group("net_bps")),
                    size_usdc_micro=int(m_new.group("size_usdc")),
                )
            )
            continue

        m_old = SCAN_RE_OLD.search(line)
        if not m_old:
            continue
        edge = float(m_old.group("edge"))
        rows.append(
            ScanRow(
                pair="SOL/USDC",
                probe_edge=edge,
                exec_edge=edge,
                cost_bps=max(0.0, edge - float(m_old.group("net_bps"))),
                net_bps=float(m_old.group("net_bps")),
                size_usdc_micro=int(m_old.group("size_usdc")),
            )
        )
    if max_rows > 0:
        return rows[-max_rows:]
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default="logs/v2.log", help="Path to scan log file")
    parser.add_argument(
        "--fallback-log",
        default="logs/bot.log",
        help="Fallback log path when primary has no scan rows",
    )
    parser.add_argument("--rows", type=int, default=1500, help="Max recent rows to replay")
    parser.add_argument(
        "--ladder",
        default=os.getenv("CEX_DEX_EXEC_SIZE_LADDER", "1.0,0.75,0.5,0.35"),
        help="Comma-separated size multipliers",
    )
    parser.add_argument("--min-net", type=float, default=_env_float("CEX_DEX_MIN_NET_SPREAD_BPS", 1.5))
    args = parser.parse_args()

    ladder: list[float] = []
    for token in str(args.ladder).split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = float(token)
        except ValueError:
            continue
        if value > 0:
            ladder.append(value)
    if not ladder:
        ladder = [1.0, 0.75, 0.5, 0.35]

    primary = Path(args.log)
    rows = _load_rows(primary, args.rows)
    source = str(primary)
    if not rows and args.fallback_log:
        fallback = Path(args.fallback_log)
        rows = _load_rows(fallback, args.rows)
        source = str(fallback)
    if not rows:
        print(f"No scan rows found in {args.log} or {args.fallback_log}")
        return

    uplift_sum = 0.0
    improved = 0
    crossed = 0
    current_pass = 0
    best_pass = 0

    for row in rows:
        current_net = row.exec_edge - _modeled_cost(row.size_usdc_micro)
        best_net = current_net
        for mult in ladder:
            alt_size = max(1_000_000, int(row.size_usdc_micro * mult))
            alt_net = row.exec_edge - _modeled_cost(alt_size)
            if alt_net > best_net:
                best_net = alt_net
        uplift = best_net - current_net
        if uplift > 0:
            improved += 1
            uplift_sum += uplift
        if current_net >= args.min_net:
            current_pass += 1
        if best_net >= args.min_net:
            best_pass += 1
            if current_net < args.min_net:
                crossed += 1

    n = len(rows)
    print("=== Exec Ladder Replay (conservative) ===")
    print(f"rows={n} log={source}")
    print(f"ladder={','.join(str(x) for x in ladder)}")
    print(f"min_net_gate={args.min_net:.2f} bps")
    print(f"improved_rows={improved} ({100.0*improved/n:.1f}%)")
    print(f"avg_uplift_all={uplift_sum/n:.3f} bps")
    print(f"avg_uplift_improved={uplift_sum/max(1, improved):.3f} bps")
    print(f"pass_before={current_pass} ({100.0*current_pass/n:.1f}%)")
    print(f"pass_after={best_pass} ({100.0*best_pass/n:.1f}%)")
    print(f"new_passes_from_ladder={crossed}")


if __name__ == "__main__":
    main()
