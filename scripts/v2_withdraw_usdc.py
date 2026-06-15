#!/usr/bin/env python3
"""Move USDC from Backpack to Ledger wallet for v2 reverse (Jupiter buy leg)."""

from __future__ import annotations

import argparse
import asyncio
import os
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
                fn.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


async def main() -> int:
    _stdio_utf8()
    parser = argparse.ArgumentParser(description="Withdraw USDC from Backpack to on-chain wallet")
    parser.add_argument(
        "--amount",
        type=float,
        default=None,
        help="USDC to withdraw (default: V2_MAX_FLASH_USDC + buffer)",
    )
    parser.add_argument("--execute", action="store_true", help="Submit withdrawal (default: dry-run)")
    args = parser.parse_args()

    from src.v2.config import V2Config

    V2Config.from_env().apply_reverse_env()
    from src.config.settings import bootstrap_config
    from src.core.wallet import get_onchain_usdc_balance, get_wallet_pubkey
    from src.cex.backpack import get_backpack_client

    bootstrap_config()
    dest = get_wallet_pubkey()
    if not dest:
        print("WALLET_PUBKEY not set in .env")
        return 1

    need = float(os.getenv("V2_MAX_FLASH_USDC", "12"))
    buffer = float(os.getenv("V2_USDC_WITHDRAW_BUFFER", "3"))
    amount = args.amount if args.amount is not None else need + buffer

    onchain = await get_onchain_usdc_balance()
    print(f"Wallet: {dest}")
    print(f"On-chain USDC now: ${onchain:.2f}")
    print(f"Target withdraw: ${amount:.2f}")

    if onchain >= need:
        print(f"Already funded (>= ${need:.0f} on-chain). Nothing to do.")
        return 0

    client = get_backpack_client()
    try:
        cex_usdc = await client.get_balance("USDC")
        print(f"Backpack USDC: ${cex_usdc:.2f}")

        min_onchain = float(os.getenv("V2_MIN_USDC_BALANCE", "12"))
        cex_reserve = float(os.getenv("V2_CEX_USDC_RESERVE", "0"))
        keep_on_cex = min(cex_reserve, max(0.5, cex_usdc * 0.1)) if cex_usdc > 2 else 0.5
        available = max(0.0, cex_usdc - keep_on_cex)

        if cex_usdc < amount:
            if available < 1.0:
                print(
                    f"Insufficient Backpack USDC for ${amount:.2f} withdraw "
                    f"(available ${available:.2f} after ${keep_on_cex:.2f} reserve)."
                )
                return 1
            amount = min(amount, available)
            print(f"Reduced withdraw to ${amount:.2f} (CEX available after reserve)")

        if not args.execute:
            print("\nDry-run only. Re-run with --execute to submit withdrawal.")
            print("Ensure your Ledger address is 2FA-exempt in Backpack withdrawal settings.")
            return 0

        result = await client.withdraw_usdc(amount, dest)
        if not result or not result.get("success"):
            print(f"Withdrawal failed: {result}")
            return 1

        wid = result.get("id") or result.get("withdrawalId") or result
        print(f"Withdrawal submitted: {wid}")
        print("Wait 1–3 min, then: npm run v2:balance")
        return 0
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            await close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
