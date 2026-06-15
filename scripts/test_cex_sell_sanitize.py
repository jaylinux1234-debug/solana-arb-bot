#!/usr/bin/env python3
"""Dry-run BackpackExecutor SOL sell sizing (no live order)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.cex.backpack import BackpackExecutor, get_backpack_client
from src.config.settings import bootstrap_config
from src.core.rpc_config import get_robust_sol_balance
from src.core.wallet import get_usdc_balance_robust, get_wallet_pubkey


async def main() -> None:
    bootstrap_config()
    pk = get_wallet_pubkey()
    bp = get_backpack_client()
    ex = BackpackExecutor(bp)

    usdc = await get_usdc_balance_robust(pk)
    sol = await get_robust_sol_balance(pk)
    bp_sol = await bp.get_balance("SOL")
    market = await ex.ensure_market()

    sample = min(0.17, max(0.0, bp_sol - 0.06))
    safe = await ex.sanitize_sol_quantity(sample)

    print(f"wallet={pk[:16]}...")
    print(f"onchain_usdc=${usdc:.2f} onchain_sol={sol:.4f} backpack_sol={bp_sol:.4f}")
    print(f"market step={market['step_size']} min={market['min_quantity']}")
    print(f"sanitize sample={sample:.6f} -> safe={safe:.4f}")
    print("dry_run_only=1 (no order placed)")


if __name__ == "__main__":
    asyncio.run(main())
