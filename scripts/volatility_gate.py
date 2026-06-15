#!/usr/bin/env python3
"""CLI: print 5m CEX vol % and whether the low-vol skip gate would fire."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config.settings import bootstrap_config
from src.cex.backpack import BackpackClient
from src.strategies.volatility_gate import (
    get_5m_volatility_pct,
    record_cex_price,
    should_skip_low_vol_cycle,
)


async def main() -> None:
    bootstrap_config()
    bp = BackpackClient()
    try:
        buy, _mid, _ask = await bp.get_cex_buy_reference_price("SOL_USDC")
        if buy and buy > 0:
            record_cex_price(float(buy))
        vol = get_5m_volatility_pct()
        gross = float(os.getenv("VOL_GATE_PROBE_GROSS_BPS", "0"))
        print(f"5m_vol_pct={vol}")
        print(f"skip_low_vol={should_skip_low_vol_cycle(vol, gross)}")
    finally:
        await bp.close()


if __name__ == "__main__":
    asyncio.run(main())
