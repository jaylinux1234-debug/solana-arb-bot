#!/usr/bin/env python3
"""
Live breakeven simulator — Monte Carlo PnL probability at current gates.

Uses latest trade/near-miss stats + env cost model (no live CEX/Jupiter calls by default).
Pass ``--live-quotes`` to fetch current Backpack ask + Jupiter sell probe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _modeled_cost_bps() -> float:
    if os.getenv("CEX_DEX_USE_COMPONENT_COST_MODEL", "").lower() in ("1", "true", "yes"):
        keys = (
            "CEX_DEX_CEX_FEE_ROUNDTRIP_BPS",
            "CEX_DEX_JUPITER_LEG_FEE_BUFFER_BPS",
            "CEX_DEX_EXECUTION_SLIPPAGE_BUFFER_BPS",
            "CEX_DEX_WITHDRAWAL_LATENCY_BPS",
        )
        return sum(_env_float(k, 0) for k in keys)
    return _env_float("CEX_DEX_STRATEGY_BASE_COST_BPS", 14.0)


async def fetch_live_gross_bps() -> float | None:
    from src.config.settings import bootstrap_config
    from src.cex.backpack import BackpackClient
    from src.dex.jupiter import JupiterClient, SOL_MINT
    from src.utils.price import bps_diff

    settings = bootstrap_config()
    bp = BackpackClient(settings)
    jup = JupiterClient(settings)
    try:
        cex_buy, _, _ = await bp.get_cex_buy_reference_price("SOL_USDC")
        probe = int(os.getenv("CEX_DEX_PROBE_USDC_MICRO", "12000000"))
        sell_px, _ = await jup.get_implied_usdc_per_base_sell(
            probe,
            SOL_MINT,
            float(cex_buy or 0),
            base_decimals=9,
        )
        if not cex_buy or not sell_px:
            return None
        return abs(float(bps_diff(float(cex_buy), float(sell_px))))
    finally:
        for client in (bp, jup):
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    pass
            session = getattr(client, "session", None)
            if session is not None and hasattr(session, "close"):
                try:
                    await session.close()
                except Exception:
                    pass


def monte_carlo(
    gross_bps: float,
    *,
    paths: int = 1000,
    cost_bps: float | None = None,
    slippage_std_bps: float = 3.0,
    min_net_gate: float | None = None,
) -> dict:
    cost = cost_bps if cost_bps is not None else _modeled_cost_bps()
    min_net = min_net_gate if min_net_gate is not None else _env_float("CEX_DEX_MIN_NET_SPREAD_BPS", 5.0)
    min_gross = _env_float("CEX_DEX_MIN_GROSS_SPREAD_BPS", 12.0)

    positives = 0
    nets: list[float] = []
    for _ in range(paths):
        slip = random.gauss(0, slippage_std_bps)
        realized_gross = gross_bps + slip
        net = realized_gross - cost
        nets.append(net)
        if net >= min_net and realized_gross >= min_gross:
            positives += 1

    return {
        "paths": paths,
        "gross_bps_assumed": round(gross_bps, 2),
        "cost_bps": round(cost, 2),
        "min_net_gate": min_net,
        "min_gross_gate": min_gross,
        "prob_positive_at_gates": round(positives / paths, 4),
        "mean_net_bps": round(sum(nets) / len(nets), 2),
        "p10_net_bps": round(sorted(nets)[int(0.10 * paths)], 2),
        "p90_net_bps": round(sorted(nets)[int(0.90 * paths)], 2),
    }


async def main_async(args: argparse.Namespace) -> int:
    gross = args.gross_bps
    if gross is None:
        if args.live_quotes:
            gross = await fetch_live_gross_bps()
        if gross is None:
            from src.strategies.adaptive_router import load_recent_trades

            rows = load_recent_trades(hours=4.0)
            nets = [float(r.get("gross_bps") or 0) for r in rows if r.get("gross_bps")]
            gross = sum(nets) / len(nets) if nets else 0.0

    result = monte_carlo(float(gross or 0), paths=args.paths)
    print(json.dumps(result, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Breakeven Monte Carlo simulator")
    parser.add_argument("--paths", type=int, default=1000)
    parser.add_argument("--gross-bps", type=float, default=None)
    parser.add_argument("--live-quotes", action="store_true")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
