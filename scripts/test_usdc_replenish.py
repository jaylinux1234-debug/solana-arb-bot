#!/usr/bin/env python3
"""One-shot USDC replenish test (CEX withdraw → SOL swap fallback)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


async def main() -> None:
    from src.v2.config import V2Config
    from src.config.settings import bootstrap_config
    from src.cex.backpack import BackpackClient
    from src.dex.jupiter import JupiterClient
    from src.v2.usdc_manager import USDCManager
    from src.core.wallet import get_wallet_pubkey, initialize_wallet

    bootstrap_config()
    cfg = V2Config.from_env()
    cfg.apply_reverse_env()
    bootstrap_config()
    await initialize_wallet()
    bp = BackpackClient(bootstrap_config())
    jup = JupiterClient(bootstrap_config())
    mgr = USDCManager(cfg)
    try:
        before = await mgr.get_available_usdc()
        print(f"before=${before:.2f}")
        after, note = await mgr.replenish_usdc_for_trade(
            bp, jup, wallet_pubkey=get_wallet_pubkey()
        )
        print(f"after=${after:.2f} note={note}")
    finally:
        await bp.close()
        await jup.close()


if __name__ == "__main__":
    asyncio.run(main())
