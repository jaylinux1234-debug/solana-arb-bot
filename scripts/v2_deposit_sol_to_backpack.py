#!/usr/bin/env python3
"""Deposit SOL from on-chain wallet to Backpack CEX inventory."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


async def main() -> int:
    parser = argparse.ArgumentParser(description="Transfer on-chain SOL to Backpack")
    parser.add_argument(
        "--amount",
        type=float,
        default=0.20,
        help="SOL to deposit (default: 0.20)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit on-chain transfer (default: dry-run)",
    )
    args = parser.parse_args()

    from src.config.settings import bootstrap_config
    from src.cex.backpack import get_backpack_client
    from src.v2.config import V2Config
    from src.v2.inventory_manager import InventoryManager

    bootstrap_config()
    V2Config.from_env().apply_reverse_env()
    cfg = V2Config.from_env()
    bp = get_backpack_client()
    inv = InventoryManager(cfg, bp)
    try:
        cex_before = await inv.get_backpack_sol(bp)
        wallet_sol = await inv.get_wallet_sol(None)
        print(f"Wallet SOL:    {wallet_sol:.6f}")
        print(f"Backpack SOL:  {cex_before:.6f}")
        print(f"Deposit amount: {args.amount:.6f} SOL")

        if not args.execute:
            print("\nDry-run only. Re-run with --execute to send SOL to Backpack.")
            return 0

        result = await inv.deposit_wallet_sol_to_backpack(bp, args.amount)
        cex_after = await inv.get_backpack_sol(bp)
        print(f"Result: {result}")
        print(f"Backpack SOL after: {cex_after:.6f}")
        if result.get("success"):
            tx = result.get("tx_sig") or result.get("signature")
            if tx:
                print(f"Tx: https://solscan.io/tx/{tx}")
            return 0
        return 1
    finally:
        close = getattr(bp, "close", None)
        if callable(close):
            await bp.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
