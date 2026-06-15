# tests/test_integration.py
"""
Integration Test for CEX-DEX Strategy
"""

import asyncio
import pytest
from src.config.settings import bootstrap_config, get_settings
from src.strategies.cex_dex_core import CexDexCore


@pytest.mark.asyncio
async def test_strategy_scan():
    bootstrap_config()
    settings = get_settings()
    settings.TEST_MODE = True
    settings.SIMULATE = True

    strategy = CexDexCore()
    
    print("🔍 Testing opportunity scan...")
    opp = await strategy.scan()
    
    if opp:
        print(f"✅ Opportunity found: {opp.net_bps:.1f}bps | AI: {opp.confidence}%")
        success = await strategy.execute(opp)
        print(f"Execution result: {'✅ Success' if success else '❌ Failed'}")
    else:
        print("ℹ️  No opportunity in this test cycle (normal)")


if __name__ == "__main__":
    asyncio.run(test_strategy_scan())