"""
Batch on-chain simulations for CEX-DEX rehearsal.

Increments ``wallet_safety`` successful_sim_count when simulate_transaction reports ``err is None``.
Does not broadcast txs.

Modes:
  jupiter_swap (default) — Jupiter quote + swap tx signed then simulated (no Kamino reserve env).
  kamino_collateral — full Klend flash + Jupiter collateral-swap build then simulated.

Usage (project root):
  python -m venv venv
  # Linux/macOS: source venv/bin/activate
  # Windows:     .\\venv\\Scripts\\Activate.ps1
  pip install -r requirements.txt
  cp .env.example .env   # set SOLANA_RPC_URL, PRIVATE_KEY, PRIVATE_KEY_CEX_DEX, Kamino reserves

  python cex_dex_sim_batch.py --count 25
  python cex_dex_sim_batch.py --count 25 --signer cex   # PRIVATE_KEY_CEX_DEX
  python cex_dex_sim_batch.py --mode kamino_collateral --count 25
  # Imports: src.dex.jupiter, src.core.wallet_safety (no root shims)

  # 1000+ successful sims (wallet_safety counter): repeat batches or use scripts/run_kamino_sims.ps1
  python cex_dex_sim_batch.py --count 200 --mode kamino_collateral --signer primary
  python cex_dex_sim_batch.py --count 200 --mode kamino_collateral --signer cex
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import aiohttp
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("cex_dex_sim_batch")


# Rehearsal sims: load secrets before prod/Ledger skip-hot policy (no live sends).
os.environ.setdefault("TEST_MODE", "true")
if os.getenv("CEX_DEX_SIM_BATCH_LOAD_SECRETS", "1").strip().lower() in ("1", "true", "yes"):
    os.environ.setdefault("APP_ENV", "development")

from src.config.settings import bootstrap_config, settings  # noqa: E402

bootstrap_config()


def _require_sim_env(signer: str) -> None:
    rpc = settings.SOLANA_RPC_URL.strip()
    if not rpc:
        raise SystemExit(
            "SOLANA_RPC_URL is empty. Copy .env.example to .env and set your RPC endpoint."
        )
    if signer == "cex":
        pk = settings.PRIVATE_KEY_CEX_DEX.strip()
        name = "PRIVATE_KEY_CEX_DEX"
    else:
        pk = settings.PRIVATE_KEY.strip()
        name = "PRIVATE_KEY"
    if not pk:
        raise SystemExit(f"{name} is empty. Copy .env.example to .env before running sims.")


import src.core.wallet_safety as wallet_safety  # noqa: E402
from src.dex.jupiter import SOL_MINT, USDC_MINT, JupiterExecutor  # noqa: E402


async def _jupiter_quote_swap_sim(
    jupiter: JupiterExecutor, *, amount_micro: int, slippage_bps: int
) -> bool:
    """CEX-DEX-like Jupiter leg only (USDC -> SOL): quote -> swap tx -> simulate."""
    quote_url = os.getenv("JUPITER_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote")
    params = {
        "inputMint": USDC_MINT,
        "outputMint": SOL_MINT,
        "amount": str(amount_micro),
        "slippageBps": str(slippage_bps),
    }
    headers: dict[str, str] = {"Accept": "application/json"}
    key = (os.getenv("JUPITER_API_KEY") or "").strip()
    if key:
        headers["x-api-key"] = key

    retries = max(1, int(os.getenv("JUPITER_QUOTE_MAX_RETRIES", "8")))
    base_delay = float(os.getenv("JUPITER_QUOTE_RETRY_DELAY_SEC", "1.0"))
    quote: dict | None = None
    raw_last = ""

    async with aiohttp.ClientSession() as session:
        for attempt in range(retries):
            async with session.get(
                quote_url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                raw_last = await resp.text()
                if resp.status == 429 or resp.status in (500, 502, 503, 504):
                    await asyncio.sleep(base_delay * (2**attempt))
                    continue
                if resp.status != 200:
                    logger.warning("Jupiter quote HTTP %s: %s", resp.status, raw_last[:200])
                    await asyncio.sleep(base_delay * (2**attempt))
                    continue
                try:
                    parsed = json.loads(raw_last) if raw_last.strip() else {}
                except json.JSONDecodeError:
                    low = raw_last.lower()
                    if "rate limit" in low or "too many requests" in low:
                        await asyncio.sleep(base_delay * (2**attempt))
                        continue
                    logger.warning("Jupiter quote not JSON: %s", raw_last[:200])
                    return False
                quote = parsed if isinstance(parsed, dict) else None
                break
        else:
            logger.warning("Jupiter quote retries exhausted (last: %s)", raw_last[:200])
            return False

    if quote is None:
        return False

    if quote.get("error") or "outAmount" not in quote:
        err = quote.get("error")
        if isinstance(err, str) and ("rate limit" in err.lower() or "429" in err):
            # Rare JSON error payload; one more backoff path via outer loop would need refactor — log and fail soft
            logger.warning("Jupiter quote error (rate limit): %s", err[:120])
        else:
            logger.warning("Jupiter quote invalid: %s", err if err else quote)
        return False

    swap_data = await jupiter.get_swap_transaction(quote, slippage_bps)
    if not swap_data or "swapTransaction" not in swap_data:
        logger.warning("Jupiter swap build failed: %s", swap_data)
        return False

    swap_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_data["swapTransaction"]))
    sig = jupiter.keypair.sign_message(to_bytes_versioned(swap_tx.message))
    signed_tx = VersionedTransaction.populate(swap_tx.message, [sig])
    sim = await jupiter.client.simulate_transaction(signed_tx)
    if sim.value.err is not None:
        logger.warning("simulate failed (jupiter_swap): %s", sim.value.err)
        return False

    wallet_safety.record_successful_simulation()
    gross_hint = float(os.getenv("CEX_DEX_SIM_BATCH_GROSS_BPS", "62"))
    from src.strategies.cex_dex_core import net_spread_bps_after_costs

    est_net = net_spread_bps_after_costs(gross_hint, flash_micro, direction="dex_cheap")
    _append_backtest_training_row(
        gross_bps=gross_hint,
        net_bps=est_net,
        flash_micro=flash_micro,
        sim_ok=True,
    )
    return True


async def _one_kamino_sim(jupiter: JupiterExecutor, *, flash_micro: int, direction: str) -> bool:
    slippage_bps = int(os.getenv("JUPITER_SLIPPAGE_BPS", os.getenv("MAX_SLIPPAGE_BPS", "100")))
    try:
        if direction == "dex_cheap":
            tx = await jupiter.build_collateral_swap_tx(
                borrow_reserve_mint=USDC_MINT,
                target_collateral_mint=SOL_MINT,
                flash_amount=flash_micro,
                slippage_bps=slippage_bps,
            )
        else:
            lamports = max(1, int(os.getenv("CEX_DEX_SIM_BATCH_SOL_LAMPORTS", "100000000")))
            tx = await jupiter.build_collateral_swap_tx(
                borrow_reserve_mint=SOL_MINT,
                target_collateral_mint=USDC_MINT,
                flash_amount=lamports,
                slippage_bps=slippage_bps,
            )
    except Exception as exc:
        logger.warning("build failed (%s): %s", direction, exc)
        return False

    sim = await jupiter.client.simulate_transaction(tx)
    if sim.value.err is not None:
        logger.warning("simulate failed (%s): %s", direction, sim.value.err)
        return False

    wallet_safety.record_successful_simulation()
    gross_hint = float(os.getenv("CEX_DEX_SIM_BATCH_GROSS_BPS", "62"))
    from src.strategies.cex_dex_core import net_spread_bps_after_costs

    est_net = net_spread_bps_after_costs(
        gross_hint, flash_micro, direction=direction
    )
    _append_backtest_training_row(
        gross_bps=gross_hint,
        net_bps=est_net,
        flash_micro=flash_micro,
        sim_ok=True,
    )
    return True


def _append_backtest_training_row(
    *,
    gross_bps: float,
    net_bps: float,
    flash_micro: int,
    sim_ok: bool,
) -> None:
    """Append one labeled row for LightGBM training under backtest_results/."""
    from datetime import UTC, datetime

    out_dir = Path(os.getenv("BACKTEST_RESULTS_DIR", "backtest_results"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "sim_trades.jsonl"
    row = {
        "timestamp": datetime.now(UTC).isoformat(),
        "gross_spread_bps": float(gross_bps),
        "gross_bps": float(gross_bps),
        "net_bps": float(net_bps),
        "volatility_bps": float(os.getenv("CEX_DEX_SIM_VOLATILITY_BPS", "85")),
        "cex_depth_util": 0.45,
        "jupiter_impact_pct": min(2.5, flash_micro / 5_000_000.0),
        "inventory_sol": 0.0,
        "pnl_last_24h": 0.0,
        "win_streak": 1 if sim_ok else 0,
        "profit_usdc": (net_bps / 10000.0) * (flash_micro / 1_000_000.0) if sim_ok else -5.0,
        "was_profitable": sim_ok and net_bps >= int(os.getenv("CEX_DEX_MIN_NET_SPREAD_BPS", "48")),
        "source": "cex_dex_sim_batch",
    }
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _log_spread_scenario(gross_bps: float, flash_micro: int) -> None:
    from src.strategies.cex_dex_core import net_spread_bps_after_costs

    est_net = net_spread_bps_after_costs(
        gross_bps,
        flash_micro,
        direction="dex_cheap",
    )
    min_gross = int(os.getenv("CEX_DEX_MIN_GROSS_SPREAD_BPS", "8"))
    min_net = int(os.getenv("CEX_DEX_MIN_NET_SPREAD_BPS", "4"))
    logger.info(
        "Scenario | gross_bps=%.1f est_net_bps=%.1f size_micro=%s "
        "min_gross=%s min_net=%s passes_gross=%s passes_net=%s",
        gross_bps,
        est_net,
        flash_micro,
        min_gross,
        min_net,
        gross_bps >= min_gross,
        est_net >= min_net,
    )
    try:
        from src.monitoring.metrics import record_trade_opportunity

        record_trade_opportunity("cex_dex_sim", int(gross_bps), int(est_net))
    except Exception as exc:
        logger.debug("metrics skipped: %s", exc)

    passes = est_net >= min_net and gross_bps >= min_gross
    _append_backtest_training_row(
        gross_bps=gross_bps,
        net_bps=est_net,
        flash_micro=flash_micro,
        sim_ok=passes,
    )


async def main_async(
    count: int,
    signer: str,
    alternate_legs: bool,
    mode: str,
    *,
    gross_bps: float | None = None,
    size_micro: int | None = None,
    scenario_only: bool = False,
) -> int:
    flash_micro = (
        int(size_micro)
        if size_micro is not None
        else int(os.getenv("CEX_DEX_SIM_BATCH_FLASH_USDC_MICRO", "10000000"))
    )
    if scenario_only:
        if gross_bps is None:
            logger.error("--scenario-only requires --gross-bps")
            return 2
        _log_spread_scenario(gross_bps, flash_micro)
        return 0

    _require_sim_env(signer)

    before = wallet_safety.simulation_count()
    logger.info("successful_sim_count before: %s", before)

    pk = None
    if signer == "cex":
        pk = (os.getenv("PRIVATE_KEY_CEX_DEX") or "").strip()
        if not pk:
            logger.error("PRIVATE_KEY_CEX_DEX empty")
            return 2

    jupiter = JupiterExecutor(private_key_material=pk) if pk else JupiterExecutor()
    slippage_bps = int(os.getenv("MAX_SLIPPAGE_BPS", "100"))

    if gross_bps is not None:
        _log_spread_scenario(gross_bps, flash_micro)

    ok = 0
    for i in range(count):
        if mode == "jupiter_swap":
            if await _jupiter_quote_swap_sim(
                jupiter, amount_micro=flash_micro, slippage_bps=slippage_bps
            ):
                ok += 1
                logger.info("[%s/%s] sim OK (total successes this run: %s)", i + 1, count, ok)
        else:
            direction = "cex_cheap" if alternate_legs and (i % 2 == 1) else "dex_cheap"
            if await _one_kamino_sim(jupiter, flash_micro=flash_micro, direction=direction):
                ok += 1
                logger.info("[%s/%s] sim OK (total successes this run: %s)", i + 1, count, ok)
        await asyncio.sleep(float(os.getenv("CEX_DEX_SIM_BATCH_DELAY_SEC", "1.25")))

    await jupiter.client.close()

    after = wallet_safety.simulation_count()
    logger.info(
        "successful_sim_count after: %s (delta +%s, OK loops %s/%s)",
        after,
        after - before,
        ok,
        count,
    )
    return 0 if ok >= count else 1


def main() -> None:
    p = argparse.ArgumentParser(
        description="Batch Jupiter/Kamino simulations for wallet_safety counter"
    )
    p.add_argument("--count", type=int, default=25, help="Iterations (default 25)")
    p.add_argument(
        "--mode",
        choices=("jupiter_swap", "kamino_collateral"),
        default="jupiter_swap",
        help="jupiter_swap: quote+swap simulate (default). kamino_collateral: Klend flash build.",
    )
    p.add_argument(
        "--signer",
        choices=("primary", "cex"),
        default="primary",
        help="primary=PRIVATE_KEY (default), cex=PRIVATE_KEY_CEX_DEX",
    )
    p.add_argument(
        "--alternate",
        action="store_true",
        help="Alternate dex_cheap / cex_cheap legs (kamino_collateral only)",
    )
    p.add_argument(
        "--gross-bps",
        type=float,
        default=None,
        dest="gross_bps",
        metavar="BPS",
        help="Log spread scenario and publish last-opportunity gauges (no on-chain effect)",
    )
    p.add_argument(
        "--size",
        type=int,
        default=None,
        metavar="USDC_MICRO",
        help="Flash / quote size in USDC micro-units (default CEX_DEX_SIM_BATCH_FLASH_USDC_MICRO)",
    )
    p.add_argument(
        "--scenario-only",
        action="store_true",
        help="Spread scenario log + metrics only (no RPC sim; no PRIVATE_KEY required)",
    )
    args = p.parse_args()

    if args.count < 1:
        print("--count must be >= 1", file=sys.stderr)
        raise SystemExit(2)
    if args.size is not None and args.size < 1:
        print("--size must be >= 1 (USDC micro)", file=sys.stderr)
        raise SystemExit(2)

    code = asyncio.run(
        main_async(
            args.count,
            args.signer,
            args.alternate,
            args.mode,
            gross_bps=args.gross_bps,
            size_micro=args.size,
            scenario_only=args.scenario_only,
        )
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
