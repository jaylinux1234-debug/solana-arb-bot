#!/usr/bin/env python3
"""Deposit USDC from on-chain SPL wallet to Backpack (CEX buy leg funding)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transfer on-chain USDC to Backpack deposit address",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=float(os.getenv("V2_BACKPACK_USDC_DEPOSIT_AMOUNT", "50")),
        help="USDC to send (default: 50)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit on-chain transfer (default: dry-run)",
    )
    parser.add_argument(
        "--settle-sec",
        type=float,
        default=float(os.getenv("V2_CEX_USDC_DEPOSIT_SETTLE_SEC", "90")),
        help="Seconds to wait for Backpack credit",
    )
    args = parser.parse_args()

    from src.config.settings import bootstrap_config
    from src.cex.backpack import get_backpack_client
    from src.core.wallet import (
        get_onchain_usdc_balance,
        get_wallet_pubkey,
        transfer_usdc,
    )

    bootstrap_config()
    wallet = get_wallet_pubkey()
    if not wallet:
        print("WALLET_PUBKEY not set")
        return 1

    amount = float(args.amount)
    if amount < 1.0:
        print("Amount must be >= $1")
        return 1

    on_chain = await get_onchain_usdc_balance()
    client = get_backpack_client()
    try:
        cex_before = float(await client.get_balance("USDC"))
        dep = await client.get_deposit_address("Solana")
        if not dep.get("success"):
            print(f"Backpack deposit address failed: {dep}")
            return 1
        dest = str(dep.get("address") or "").strip()
        if not dest:
            print("Backpack deposit address empty")
            return 1

        print(f"Wallet: {wallet}")
        print(f"On-chain USDC:  ${on_chain:.2f}")
        print(f"Backpack USDC:  ${cex_before:.2f}")
        print(f"Deposit address: {dest}")
        print(f"Transfer amount: ${amount:.2f}")

        if not args.execute:
            print("\nDry-run only. Re-run with --execute to send USDC to Backpack.")
            return 0

        result = await transfer_usdc(amount, dest)
        if not result.get("success"):
            print(f"Transfer failed: {result}")
            return 1

        tx_sig = result.get("tx_sig")
        print(f"On-chain transfer submitted: {tx_sig}")
        print(f"Waiting up to {args.settle_sec:.0f}s for Backpack credit…")

        target = cex_before + amount * 0.98
        deadline = asyncio.get_event_loop().time() + max(10.0, args.settle_sec)
        cex_after = cex_before
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(10.0)
            if hasattr(client, "clear_balance_cache"):
                client.clear_balance_cache("USDC")
            cex_after = float(await client.get_balance("USDC"))
            print(f"  Backpack USDC: ${cex_after:.2f} (target >= ${target:.2f})")
            if cex_after >= target:
                break

        on_chain_after = await get_onchain_usdc_balance()
        print(f"\nDone.")
        print(f"On-chain USDC:  ${on_chain_after:.2f}")
        print(f"Backpack USDC:  ${cex_after:.2f}")
        if tx_sig:
            print(f"Tx: https://solscan.io/tx/{tx_sig}")
        return 0 if cex_after >= target else 2
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            await close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
