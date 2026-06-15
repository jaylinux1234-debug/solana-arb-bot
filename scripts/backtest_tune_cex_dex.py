#!/usr/bin/env python3
"""
Grid-search CEX-DEX gross/size params using spread scenarios + optional Helius archive.

Supports multi-pair tuning via ``CEX_MIDCAPS`` (see ``src/cex/trading_pairs.py``).

Usage:
  python scripts/backtest_tune_cex_dex.py --scenarios-only
  python scripts/backtest_tune_cex_dex.py --all-pairs --scenarios-only
  python scripts/backtest_tune_cex_dex.py --pair WIF --gross-min 8 --gross-max 22
  python scripts/backtest_tune_cex_dex.py --helius-hours 24
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("backtest_tune")


def _run_scenario(gross_bps: float, size_micro: int, *, symbol: str = "SOL") -> dict:
    from src.strategies.cex_dex_core import net_spread_bps_after_costs

    est_net = net_spread_bps_after_costs(gross_bps, size_micro, direction="cex_cheap")
    min_gross = int(os.getenv("CEX_DEX_MIN_GROSS_SPREAD_BPS", "8"))
    min_net = int(os.getenv("CEX_DEX_MIN_NET_SPREAD_BPS", "3"))
    passes = gross_bps >= min_gross and est_net >= min_net
    return {
        "symbol": symbol,
        "gross_bps": gross_bps,
        "est_net_bps": est_net,
        "size_micro": size_micro,
        "passes_gates": passes,
    }


def run_scenario_grid(
    gross_min: int,
    gross_max: int,
    gross_step: int,
    size_micro: int,
    *,
    symbol: str = "SOL",
) -> list[dict]:
    results: list[dict] = []
    for gross in range(gross_min, gross_max + 1, gross_step):
        row = _run_scenario(float(gross), size_micro, symbol=symbol)
        results.append(row)
        logger.info(
            "%s gross=%s net_est=%.1f passes=%s",
            symbol,
            gross,
            row["est_net_bps"],
            row["passes_gates"],
        )
    return results


def recommend_params(results: list[dict]) -> dict:
    passing = [r for r in results if r["passes_gates"]]
    if not passing:
        best = max(results, key=lambda r: r["est_net_bps"])
        return {
            "recommended_gross_bps": best["gross_bps"],
            "recommended_note": "no_gate_pass; picked max est_net",
            "passing_count": 0,
        }
    passing.sort(key=lambda r: (r["gross_bps"], -r["est_net_bps"]))
    pick = passing[0]
    return {
        "recommended_gross_bps": pick["gross_bps"],
        "recommended_est_net_bps": pick["est_net_bps"],
        "passing_count": len(passing),
        "passing_range_gross_bps": [passing[0]["gross_bps"], passing[-1]["gross_bps"]],
    }


def tune_pairs(
    symbols: list[str],
    gross_min: int,
    gross_max: int,
    gross_step: int,
    size_micro: int,
) -> dict[str, dict]:
    from src.cex.trading_pairs import load_cex_dex_pairs

    by_sym = {p.symbol: p for p in load_cex_dex_pairs()}
    out: dict[str, dict] = {}
    for sym in symbols:
        sym_u = sym.strip().upper()
        if sym_u not in by_sym:
            logger.warning("Skip unknown pair %s (no mint in registry)", sym_u)
            continue
        grid = run_scenario_grid(
            gross_min, gross_max, gross_step, size_micro, symbol=sym_u
        )
        out[sym_u] = {
            "pair_label": by_sym[sym_u].pair_label,
            "grid": grid,
            "recommendation": recommend_params(grid),
        }
    return out


async def fetch_helius_volatility_proxy(hours: int) -> dict | None:
    api_key = (os.getenv("HELIUS_API_KEY") or "").strip()
    if not api_key:
        logger.info("HELIUS_API_KEY unset — skip archive fetch")
        return None

    try:
        import httpx
    except ImportError:
        return None

    url = f"https://api.helius.xyz/v0/addresses/{os.getenv('WALLET_PUBKEY', '')}/transactions"
    if not os.getenv("WALLET_PUBKEY"):
        logger.info("WALLET_PUBKEY unset — skip Helius address history")
        return None

    params = {"api-key": api_key, "limit": min(100, hours * 4)}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.warning("Helius history HTTP %s", resp.status_code)
            return None
        data = resp.json()
        return {"tx_sample_count": len(data) if isinstance(data, list) else 0}


def run_jupiter_sim_batch(count: int, gross_bps: float, size_micro: int) -> int:
    py = sys.executable
    cmd = [
        py,
        str(ROOT / "src" / "scripts" / "cex_dex_sim_batch.py"),
        "--count",
        str(count),
        "--mode",
        "jupiter_swap",
        "--gross-bps",
        str(gross_bps),
        "--size",
        str(size_micro),
    ]
    logger.info("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)})
    return int(proc.returncode or 0)


def main() -> int:
    from src.config.settings import bootstrap_config
    from src.cex.trading_pairs import load_cex_dex_pairs

    bootstrap_config()

    p = argparse.ArgumentParser(description="Tune CEX-DEX params (multi-pair)")
    p.add_argument("--scenarios-only", action="store_true", help="Spread math only (no RPC)")
    p.add_argument("--all-pairs", action="store_true", help="Tune every loaded CEX_MIDCAPS + SOL")
    p.add_argument("--pair", action="append", dest="pairs", help="Symbol e.g. WIF (repeatable)")
    p.add_argument("--gross-min", type=int, default=8)
    p.add_argument("--gross-max", type=int, default=25)
    p.add_argument("--gross-step", type=int, default=1)
    p.add_argument("--size", type=int, default=20_000_000, help="USDC micro")
    p.add_argument("--sim-count", type=int, default=0, help="Jupiter sim batch after grid (SOL only)")
    p.add_argument("--helius-hours", type=int, default=0)
    args = p.parse_args()

    if args.all_pairs:
        symbols = [p.symbol for p in load_cex_dex_pairs()]
    elif args.pairs:
        symbols = args.pairs
    else:
        symbols = ["SOL"]

    if len(symbols) == 1:
        grid = run_scenario_grid(
            args.gross_min, args.gross_max, args.gross_step, args.size, symbol=symbols[0]
        )
        payload: dict = {
            "symbols": symbols,
            "grid": grid,
            "recommendation": recommend_params(grid),
            "size_micro": args.size,
        }
    else:
        per_pair = tune_pairs(
            symbols, args.gross_min, args.gross_max, args.gross_step, args.size
        )
        payload = {
            "symbols": symbols,
            "per_pair": per_pair,
            "size_micro": args.size,
        }

    if args.helius_hours > 0:
        helius = asyncio.run(fetch_helius_volatility_proxy(args.helius_hours))
        if helius:
            payload["helius_proxy"] = helius

    if not args.scenarios_only and args.sim_count > 0 and len(symbols) == 1:
        rec = payload.get("recommendation") or {}
        if isinstance(payload.get("per_pair"), dict):
            rec = payload["per_pair"].get(symbols[0], {}).get("recommendation", {})
        gross = float(rec.get("recommended_gross_bps", 15))
        code = run_jupiter_sim_batch(args.sim_count, gross, args.size)
        payload["sim_exit_code"] = code

    out_dir = Path(os.getenv("BACKTEST_RESULTS_DIR", "backtest_results"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "param_tune_latest.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Wrote %s", out_path)
    print(json.dumps(payload.get("recommendation") or payload.get("per_pair"), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
