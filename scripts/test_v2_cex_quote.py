#!/usr/bin/env python3
"""Test Backpack bid + Jupiter price for v2 reverse lane."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config.settings import bootstrap_config
from src.cex.backpack import BackpackClient
from src.dex.jupiter import JupiterClient
from src.core.risk import RiskEngine
from src.strategies.dex_cex_reverse import DexCexReverseStrategy
from src.v2.config import V2Config
from src.v2.dex_cex_reverse import V2ReverseLane


def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        fn = getattr(stream, "reconfigure", None)
        if callable(fn):
            try:
                fn(encoding="utf-8", errors="replace")
            except Exception:
                pass


async def main() -> None:
    _stdio_utf8()
    cfg = V2Config.from_env()
    cfg.apply_reverse_env()
    settings = bootstrap_config()
    risk = RiskEngine(settings)
    backpack = BackpackClient(settings)
    jupiter = JupiterClient(settings)
    wallet = settings.wallet_pubkey or settings.WALLET_PUBKEY or ""
    reverse = DexCexReverseStrategy(
        jupiter_executor=jupiter,
        backpack_client=backpack,
        wallet_pubkey=wallet,
        settings=settings,
        risk=risk,
    )
    lane = V2ReverseLane(reverse, cfg)

    bid = await lane.get_bid_price("SOL")
    jup = await lane.get_jupiter_sol_price()
    print(f"Backpack BID (SOL/USDC): {bid}")
    print(f"Jupiter implied USDC/SOL: {jup}")
    if bid and jup:
        from src.strategies.cex_dex_core import analyze_cex_dex_spread

        sp = analyze_cex_dex_spread(float(bid), float(jup))
        print(f"Spread direction: {sp.direction if sp else 'n/a'}")
        if sp:
            print(f"Gross bps (abs): {sp.spread_bps_abs}")

    signal = await lane.detect_dex_cheap_signal()
    print(f"Executable signal: {'yes' if signal else 'no'}")
    if signal:
        print(signal)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
