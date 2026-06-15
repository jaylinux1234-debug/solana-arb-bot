"""
Standalone CEX + Jupiter price probe (no bot loop, no chain signer required).

Loads `.env` then `.env.txt` when present (same idea as main). Uses Jupiter lite quote API.
Reference CEX leg uses public Bybit spot ticker (no API keys).

Usage:
  python fetch_cex_dex_prices.py
  python fetch_cex_dex_prices.py --probe-usdc 500000000
  python fetch_cex_dex_prices.py --extras   # optional ccxt spot check (see CCXT_EXCHANGE_ID)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import aiohttp

from src.config.settings import bootstrap_config

bootstrap_config()

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"


async def fetch_backpack(symbol: str) -> dict[str, float]:
    """Top-of-book via Backpack REST depth (symbol query param, e.g. SOL_USDC)."""
    base = "https://api.backpack.exchange/api/v1"
    url = f"{base}/depth"
    params = {"symbol": symbol, "limit": "5"}
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp,
    ):
        raw = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"backpack HTTP {resp.status}: {raw[:300]}")
        data = json.loads(raw) if raw.strip() else {}

    if not isinstance(data, dict):
        raise RuntimeError(f"backpack bad JSON: {raw[:300]}")
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    if not bids or not asks:
        raise RuntimeError(f"backpack empty book: {raw[:300]}")
    best_bid = max(float(b[0]) for b in bids if b)
    best_ask = min(float(a[0]) for a in asks if a)
    return {"bid": best_bid, "ask": best_ask}


async def fetch_bybit_rest(symbol: str) -> dict[str, float]:
    """Bybit v5 spot ticker (public). Symbol example: SOLUSDC."""
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "spot", "symbol": symbol}
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp,
    ):
        raw = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"bybit HTTP {resp.status}: {raw[:300]}")
        data = json.loads(raw) if raw.strip() else {}
    lst = (data.get("result") or {}).get("list") or []
    if not lst or not isinstance(lst[0], dict):
        raise RuntimeError(f"bybit empty ticker: {str(data)[:300]}")
    t = lst[0]
    bid = t.get("bid1Price")
    ask = t.get("ask1Price")
    if bid is None or ask is None:
        raise RuntimeError(f"bybit missing bid/ask: {str(t)[:300]}")
    return {"bid": float(bid), "ask": float(ask)}


async def fetch_jupiter_implied_usdc_per_sol(
    amount_micro: int,
) -> tuple[float | None, dict[str, Any] | None]:
    slippage_bps = int(os.getenv("MAX_SLIPPAGE_BPS", "100"))
    quote_url = os.getenv("JUPITER_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote")
    params = {
        "inputMint": USDC_MINT,
        "outputMint": SOL_MINT,
        "amount": str(amount_micro),
        "slippageBps": str(slippage_bps),
    }
    headers: dict[str, str] = {}
    key = (os.getenv("JUPITER_API_KEY") or "").strip()
    if key:
        headers["x-api-key"] = key

    async with (
        aiohttp.ClientSession() as session,
        session.get(
            quote_url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp,
    ):
        raw = await resp.text()
        try:
            quote = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return None, {"parse_error": raw[:400], "http_status": resp.status}

    if not isinstance(quote, dict) or quote.get("error") or "outAmount" not in quote:
        return None, quote if isinstance(quote, dict) else None
    out_amt = int(quote["outAmount"])
    usdc = amount_micro / 1_000_000.0
    sol = out_amt / 1_000_000_000.0
    if sol <= 0:
        return None, quote
    return usdc / sol, quote


def _spread_bps(a: float, b: float, ref: float) -> float:
    if ref <= 0:
        return 0.0
    return abs(a - b) / ref * 10000


def extras_ccxt(ref_usdc_per_sol: float) -> None:
    try:
        import ccxt  # type: ignore[import-untyped]
    except ImportError:
        print("\n[extras] ccxt not installed.")
        return

    ex_id = os.getenv("CCXT_EXCHANGE_ID", "bybit")
    try:
        ex_class = getattr(ccxt, ex_id)
    except AttributeError:
        print(f"\n[ccxt] unknown exchange id: {ex_id}")
        return

    ex = ex_class({"enableRateLimit": True})
    symbol = os.getenv("CCXT_SYMBOL", "SOL/USDC")
    try:
        t = ex.fetch_ticker(symbol)
        last = t.get("last") or t.get("close")
        bid = t.get("bid")
        ask = t.get("ask")
        print(f"\n[ccxt:{ex_id}] {symbol} last={last} bid={bid} ask={ask}")
        if bid and ask and ref_usdc_per_sol > 0:
            mid = (float(bid) + float(ask)) / 2
            print(
                f"  vs Jupiter implied USDC/SOL: spread(mid) ≈ {_spread_bps(mid, ref_usdc_per_sol, ref_usdc_per_sol):.2f} bps"
            )
    except Exception as exc:
        print(f"\n[ccxt] error: {exc}")
    finally:
        try:
            ex.close()
        except Exception:
            pass


async def run_probe(probe_usdc_micro: int, extras: bool) -> int:
    bp_symbol = (os.getenv("CEX_DEX_BACKPACK_MARKET") or "SOL_USDC").strip()
    symbol = (
        os.getenv("CEX_DEX_BYBIT_SYMBOL") or os.getenv("CEX_DEX_BINANCE_SYMBOL") or "SOLUSDC"
    ).strip()

    print(f"Probe USDC amount (micro): {probe_usdc_micro}  (~{probe_usdc_micro / 1e6:.4f} USDC)")
    print(f"Backpack symbol: {bp_symbol} | Bybit spot symbol: {symbol}")

    errs: list[str] = []

    try:
        bp = await fetch_backpack(bp_symbol)
        print(
            f"\nBackpack       bid={bp['bid']:.8f}  ask={bp['ask']:.8f}  mid={(bp['bid'] + bp['ask']) / 2:.8f}"
        )
    except Exception as e:
        errs.append(f"backpack: {e}")
        print(f"\nBackpack       FAILED: {e}")

    try:
        bb = await fetch_bybit_rest(symbol)
        print(
            f"Bybit REST     bid={bb['bid']:.8f}  ask={bb['ask']:.8f}  mid={(bb['bid'] + bb['ask']) / 2:.8f}"
        )
    except Exception as e:
        errs.append(f"bybit: {e}")
        print(f"\nBybit REST     FAILED: {e}")

    jup_price, quote = await fetch_jupiter_implied_usdc_per_sol(probe_usdc_micro)
    if jup_price is not None:
        print(f"\nJupiter (USDC->SOL @ probe) implied USDC/SOL = {jup_price:.8f}")
        if quote and os.getenv("VERBOSE_QUOTE", "").lower() in ("1", "true", "yes"):
            slim = {
                k: quote.get(k)
                for k in ("inputMint", "outputMint", "inAmount", "outAmount", "priceImpactPct")
                if k in quote
            }
            print(f"  quote keys: {json.dumps(slim, indent=2)}")
    else:
        errs.append("jupiter: no quote")
        print(f"\nJupiter        FAILED or empty quote: {quote}")

    ref_ask = None
    try:
        bb = await fetch_bybit_rest(symbol)
        ref_ask = bb["ask"]
    except Exception:
        pass
    if ref_ask is None:
        try:
            bp = await fetch_backpack(bp_symbol)
            ref_ask = bp["ask"]
        except Exception:
            ref_ask = jup_price or 1.0

    if jup_price is not None and ref_ask:
        gross = _spread_bps(jup_price, ref_ask, ref_ask)
        cheaper = "Jupiter" if jup_price < ref_ask else "CEX ask"
        print(
            f"\nGross dislocation vs reference ask ({ref_ask:.8f}): ~{gross:.2f} bps (cheaper: {cheaper})"
        )

    if extras:
        try:
            extras_ccxt(jup_price or 0.0)
        except Exception as exc:
            errs.append(f"extras ccxt: {exc}")
            print(f"\nExtras (ccxt) skipped: {exc}")

    sys.stdout.flush()
    if errs:
        print(f"\nDone with {len(errs)} source error(s): {'; '.join(errs)}")
        return 1
    print("\nAll sources OK.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Standalone CEX + Jupiter price fetcher")
    p.add_argument(
        "--probe-usdc",
        type=int,
        default=int(os.getenv("CEX_DEX_JUPITER_PROBE_USDC_MICRO", "1000000000").replace("_", "")),
        help="USDC amount in micro units (6 decimals) for Jupiter quote",
    )
    p.add_argument(
        "--extras",
        action="store_true",
        help="Also run optional ccxt spot check (CCXT_EXCHANGE_ID, default bybit)",
    )
    args = p.parse_args()

    code = asyncio.run(run_probe(args.probe_usdc, args.extras))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
