import asyncio
from typing import Any

import httpx

from src.config.settings import MAX_CONCURRENT_CHECKS, MAX_SLIPPAGE_BPS
from src.core.tokens import BASE_TOKENS, COMMON_TOKENS

JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"


async def fetch_jupiter_quote(
    client: httpx.AsyncClient,
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int = MAX_SLIPPAGE_BPS,
) -> dict[str, Any] | None:
    """Fetch one Jupiter quote for a token pair."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        "slippageBps": slippage_bps,
        "onlyDirectRoutes": "false",
    }
    try:
        response = await client.get(JUPITER_QUOTE_URL, params=params, timeout=15.0)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


async def quote_triangle(
    client: httpx.AsyncClient,
    start_symbol: str,
    common_symbol: str,
    bridge_symbol: str,
    start_amount: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Quote one full triangle path: start -> common -> bridge -> start."""
    start_mint = BASE_TOKENS[start_symbol]
    common_mint = COMMON_TOKENS[common_symbol]
    bridge_mint = BASE_TOKENS[bridge_symbol]

    async with semaphore:
        leg1 = await fetch_jupiter_quote(client, start_mint, common_mint, start_amount)
        if not leg1 or "outAmount" not in leg1:
            return {
                "path": (start_symbol, common_symbol, bridge_symbol, start_symbol),
                "success": False,
                "error": "No leg1 quote",
            }

        leg1_out = int(leg1["outAmount"])
        leg2 = await fetch_jupiter_quote(client, common_mint, bridge_mint, leg1_out)
        if not leg2 or "outAmount" not in leg2:
            return {
                "path": (start_symbol, common_symbol, bridge_symbol, start_symbol),
                "success": False,
                "error": "No leg2 quote",
            }

        leg2_out = int(leg2["outAmount"])
        leg3 = await fetch_jupiter_quote(client, bridge_mint, start_mint, leg2_out)
        if not leg3 or "outAmount" not in leg3:
            return {
                "path": (start_symbol, common_symbol, bridge_symbol, start_symbol),
                "success": False,
                "error": "No leg3 quote",
            }

        final_out = int(leg3["outAmount"])
        profit_amount = final_out - start_amount
        profit_pct = (profit_amount / start_amount) * 100 if start_amount else 0.0

        return {
            "path": (start_symbol, common_symbol, bridge_symbol, start_symbol),
            "success": True,
            "start_amount": start_amount,
            "final_amount": final_out,
            "profit_amount": profit_amount,
            "profit_pct": profit_pct,
            "quotes": (leg1, leg2, leg3),
        }


async def get_jupiter_quotes_for_all_triangles(
    start_amount: int = 1_000_000,
) -> list[dict[str, Any]]:
    """
    Fetch Jupiter quotes for every triangle using BASE_TOKENS and COMMON_TOKENS.

    Triangle shape:
      base_start -> common_token -> base_bridge -> base_start
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    tasks = []

    async with httpx.AsyncClient() as client:
        for start_symbol in BASE_TOKENS:
            for common_symbol in COMMON_TOKENS:
                for bridge_symbol in BASE_TOKENS:
                    if bridge_symbol == start_symbol:
                        continue
                    tasks.append(
                        quote_triangle(
                            client=client,
                            start_symbol=start_symbol,
                            common_symbol=common_symbol,
                            bridge_symbol=bridge_symbol,
                            start_amount=start_amount,
                            semaphore=semaphore,
                        )
                    )

        return await asyncio.gather(*tasks)
