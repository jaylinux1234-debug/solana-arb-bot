#!/usr/bin/env python3
"""
Scan all CEX_MIDCAPS + SOL pairs: Backpack mid vs Jupiter USDC→base implied price.

Usage:
  python src/scripts/multi_pair_scanner.py
  python src/scripts/multi_pair_scanner.py --probe-usdc 12000000
  PYTHONPATH=. python -m src.scripts.multi_pair_scanner
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config.settings import bootstrap_config
from src.cex.backpack import BackpackClient
from src.cex.trading_pairs import CexDexPair, load_cex_dex_pairs
from src.dex.jupiter import USDC_MINT, JupiterClient
from src.dex.jupiter_params import resolve_slippage_bps
from src.strategies.cex_dex_core import analyze_cex_dex_spread, net_spread_bps_after_costs
from src.utils.price import bps_diff

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("multi_pair_scanner")


async def scan_pair(
    pair: CexDexPair,
    *,
    cex: BackpackClient,
    jup: JupiterClient,
    probe_micro: int,
) -> dict | None:
    """Return scan row or None if prices unavailable."""
    cex_price = await cex.get_market_mid_price(pair.backpack_symbol)
    if not cex_price or cex_price <= 0:
        return None

    slippage = resolve_slippage_bps(USDC_MINT, pair.base_mint)
    jup_price, _quote = await jup.get_implied_usdc_per_base(
        probe_micro,
        pair.base_mint,
        base_decimals=pair.base_decimals,
        slippage_bps=slippage,
    )
    if not jup_price or jup_price <= 0:
        return None

    spread = analyze_cex_dex_spread(cex_price, jup_price)
    if spread is None:
        return None

    edge_bps = float(bps_diff(cex_price, jup_price))
    gross_bps = edge_bps if spread.direction == "cex_cheap" else spread.spread_bps_abs
    net_bps = net_spread_bps_after_costs(
        gross_bps,
        probe_micro,
        direction=spread.direction,
    )

    min_gross = float(os.getenv("CEX_DEX_MIN_GROSS_SPREAD_BPS", "8"))
    min_net = float(os.getenv("CEX_DEX_MIN_NET_SPREAD_BPS", "3"))
    tradeable = (
        spread.direction == "cex_cheap"
        and gross_bps >= min_gross
        and net_bps >= min_net
    )

    return {
        "pair": pair.pair_label,
        "symbol": pair.symbol,
        "cex_price": cex_price,
        "jup_price": jup_price,
        "edge_bps": edge_bps,
        "gross_bps": gross_bps,
        "net_bps": net_bps,
        "direction": spread.direction,
        "tradeable": tradeable,
    }


async def scan_pairs(
    *,
    probe_micro: int | None = None,
    symbols: list[str] | None = None,
) -> list[dict]:
    bootstrap_config()

    probe = probe_micro or int(
        os.getenv("CEX_DEX_PROBE_USDC_MICRO", "12000000")
    )

    all_pairs = load_cex_dex_pairs()
    if symbols:
        want = {s.strip().upper() for s in symbols}
        pairs = [p for p in all_pairs if p.symbol in want]
    else:
        pairs = all_pairs

    cex = BackpackClient()
    jup = JupiterClient()

    rows: list[dict] = []
    try:
        for pair in pairs:
            try:
                row = await scan_pair(pair, cex=cex, jup=jup, probe_micro=probe)
            except Exception as exc:
                logger.warning("%s | scan error: %s", pair.pair_label, exc)
                continue
            if row:
                rows.append(row)
    finally:
        await cex.close()
        await jup.close()

    rows.sort(key=lambda r: float(r["net_bps"]), reverse=True)
    return rows


def _print_table(rows: list[dict], probe_micro: int) -> None:
    print(f"\nMulti-pair scan | probe=${probe_micro / 1e6:.2f} | pairs={len(rows)}\n")
    print(
        f"{'PAIR':<12} {'DIR':<12} {'EDGE':>7} {'GROSS':>7} {'NET':>7} {'CEX':>12} {'JUP':>12} {'OK':>4}"
    )
    print("-" * 72)
    for r in rows:
        ok = "YES" if r["tradeable"] else ""
        print(
            f"{r['pair']:<12} {r['direction']:<12} "
            f"{r['edge_bps']:>7.1f} {r['gross_bps']:>7.1f} {r['net_bps']:>7.1f} "
            f"{r['cex_price']:>12.6f} {r['jup_price']:>12.6f} {ok:>4}"
        )
    if rows:
        best = rows[0]
        print(
            f"\nBest net: {best['pair']} gross={best['gross_bps']:.1f}bps "
            f"net={best['net_bps']:.1f}bps dir={best['direction']}"
        )


def _append_weekly_log(rows: list[dict], probe_micro: int, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "probe_usdc_micro": probe_micro,
        "pairs": rows,
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


async def _main_async(args: argparse.Namespace) -> int:
    rows = await scan_pairs(
        probe_micro=args.probe_usdc,
        symbols=args.symbols,
    )
    probe = args.probe_usdc or int(os.getenv("CEX_DEX_PROBE_USDC_MICRO", "12000000"))
    _print_table(rows, probe)
    if args.weekly_log:
        _append_weekly_log(rows, probe, Path(args.weekly_log))
        print(f"\nAppended weekly log: {args.weekly_log}")
    return 0 if rows else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Scan CEX vs Jupiter for all configured pairs")
    p.add_argument(
        "--probe-usdc",
        type=int,
        default=None,
        help="USDC probe size in micro-units (default: CEX_DEX_PROBE_USDC_MICRO)",
    )
    p.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Limit to symbol(s), e.g. --symbol WIF --symbol BONK",
    )
    p.add_argument(
        "--weekly-log",
        default="",
        help="Append JSONL snapshot (default for npm run scan:pairs:weekly)",
    )
    args = p.parse_args()
    if not args.weekly_log and os.getenv("MULTI_PAIR_WEEKLY_LOG"):
        args.weekly_log = os.getenv("MULTI_PAIR_WEEKLY_LOG", "")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
