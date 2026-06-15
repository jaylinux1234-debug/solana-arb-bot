#!/usr/bin/env python3
"""On-chain + CEX balances for v2 reverse (USDC needed on-chain)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


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
    from src.v2.config import V2Config

    V2Config.from_env().apply_reverse_env()
    from src.config.settings import bootstrap_config
    from src.cex.backpack import get_backpack_client
    from src.core.wallet import (
        get_onchain_usdc_balance,
        get_sol_balance,
        get_usdc_balance,
        get_wallet_pubkey,
    )
    from src.core.capital_preflight import get_ledger_sol_balance

    bootstrap_config()
    backpack = get_backpack_client()
    try:
        pk = get_wallet_pubkey()
        print(f"Wallet: {pk or '(unset)'}")
        onchain_usdc = await get_onchain_usdc_balance()
        cex_usdc = await get_usdc_balance()
        cex_sol = await get_sol_balance()
        chain_sol = await get_ledger_sol_balance()
        print(f"On-chain USDC (SPL): ${onchain_usdc:.2f}")
        print(f"Backpack USDC:       ${cex_usdc:.2f}")
        print(f"Backpack SOL:        {cex_sol:.6f}")
        print(f"On-chain SOL:        {chain_sol:.6f}")
        need = float(__import__("os").getenv("V2_MAX_FLASH_USDC", "12"))
        if onchain_usdc < need:
            print(f"\nReverse v2 needs ~${need:.0f}+ on-chain USDC to buy SOL on Jupiter.")
            print("Auto-replenish will withdraw from Backpack on the next STRONG signal.")
        else:
            print(f"\nOn-chain USDC OK for v2 max trade (${need:.0f}).")
    finally:
        await backpack.close()


if __name__ == "__main__":
    asyncio.run(main())
