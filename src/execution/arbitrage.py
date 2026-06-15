"""Jupiter quote helpers for Helius/Jito backrun (three-leg USDC → token → SOL → USDC)."""

import logging
import os

import aiohttp
from solana.rpc.async_api import AsyncClient

from src.cex.price_feed import cex_feed
from src.dex.jupiter import JupiterExecutor
from src.execution.jito import JitoHelper, configure_jito, create_jito_helper

logger = logging.getLogger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
def _jupiter_quote_api_url() -> str:
    return (
        os.getenv("JUPITER_QUOTE_URL")
        or os.getenv("JUP_QUOTE_API_URL")
        or "https://lite-api.jup.ag/swap/v1/quote"
    ).strip()


class ArbitrageDetector:
    """Thin wrapper used by Helius webhook backrun to fetch aligned Jupiter quotes."""

    def __init__(self):
        self.executor = JupiterExecutor()
        self.client = AsyncClient(os.getenv("SOLANA_RPC_URL"))
        self._jito: JitoHelper | None = None

    async def _ensure_jito(self) -> JitoHelper:
        if self._jito is None:
            configure_jito(self.executor.client, self.executor.keypair)
            self._jito = await create_jito_helper(self.executor.client, self.executor.keypair)
        return self._jito

    async def get_jupiter_route_quotes_for_backrun(
        self,
        usdc_amount: int,
        intermediate_mint: str,
        *,
        slippage_bps: int = 50,
        only_direct: bool = False,
    ) -> dict:
        """Three sequential Jupiter quotes for USDC → mint → SOL → USDC (ExactIn)."""
        async with aiohttp.ClientSession() as session:

            async def get_quote(input_mint: str, output_mint: str, amount: int):
                params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": str(amount),
                    "slippageBps": str(slippage_bps),
                    "swapMode": "ExactIn",
                }
                if not only_direct:
                    params["onlyDirectRoutes"] = "false"
                for _ in range(2):
                    try:
                        timeout = aiohttp.ClientTimeout(total=10)
                        async with session.get(
                            _jupiter_quote_api_url(), params=params, timeout=timeout
                        ) as resp:
                            return await resp.json() if resp.status == 200 else None
                    except (TimeoutError, aiohttp.ClientError):
                        continue
                return None

            q1 = await get_quote(USDC_MINT, intermediate_mint, usdc_amount)
            if not q1 or "outAmount" not in q1:
                return {}

            mid_received = int(q1["outAmount"])
            q2 = await get_quote(intermediate_mint, SOL_MINT, mid_received)
            if not q2 or "outAmount" not in q2:
                return {}

            sol_received = int(q2["outAmount"])
            q3 = await get_quote(SOL_MINT, USDC_MINT, sol_received)

            # After q3 calculation
            cex_prices = await cex_feed.get_multiple_prices(["SOL/USDC"])
            cex_sol = cex_prices.get("SOL/USDC")
            short = intermediate_mint[:6]
            if cex_sol:
                implied_sol_price = sol_received / usdc_amount  # rough
                cex_edge = abs(implied_sol_price - cex_sol) / cex_sol * 100
                if cex_edge > 0.3:  # 30+ bps edge
                    logger.warning(f"🔥 CEX EDGE {cex_edge:.2f}% on {short}")

            return {
                "quote1_usdc_to_mid": q1,
                "quote2_mid_to_sol": q2,
                "quote3_sol_to_usdc": q3,
            }
