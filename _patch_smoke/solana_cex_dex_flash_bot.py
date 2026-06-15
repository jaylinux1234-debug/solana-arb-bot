#!/usr/bin/env python3
"""
🚀 PRODUCTION CEX-DEX FLASH LOAN BOT
CCXT Live Price Monitor + Kamino Flash Loan + Jupiter + Jito
"""

import asyncio
import logging
import os
from datetime import datetime

import aiohttp
import ccxt
import wallet_safety
from dotenv import load_dotenv
from jito_helper import configure_jito, send_jito_bundle
from jupiter_executor import JupiterExecutor

# Your modules
from kamino_helper import KaminoFlashLoan
from openai_helper import ai_agent_decide
from solana.rpc.async_api import AsyncClient
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

load_dotenv()

logger = logging.getLogger(__name__)

# ========================= CONFIG =========================
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
SOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")

FLASH_AMOUNT_USDC = int(os.getenv("CEX_DEX_FLASH_AMOUNT_USDC_MICRO", "25000000"))  # 25 USDC default
MIN_SPREAD_PCT = float(os.getenv("CEX_DEX_MIN_SPREAD_PCT", "0.18"))
POLL_INTERVAL = int(os.getenv("CEX_DEX_POLL_INTERVAL", "8"))  # seconds

TEST_MODE = os.getenv("TEST_MODE", "true").lower() == "true"

# CCXT Exchange (change to 'bybit', 'okx', etc.)
exchange = ccxt.binance(
    {
        "enableRateLimit": True,
    }
)


async def get_cex_price() -> float:
    """Get real SOL/USDC price from CEX"""
    try:
        ticker = exchange.fetch_ticker("SOL/USDC")
        return float(ticker["last"])
    except Exception as e:
        print(f"CEX price fetch failed: {e}")
        return None


async def get_jupiter_effective_price(session, sol_amount_lamports: int) -> float | None:
    """Get effective DEX sell price via Jupiter"""
    url = os.getenv("JUPITER_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote")
    params = {
        "inputMint": str(SOL_MINT),
        "outputMint": str(USDC_MINT),
        "amount": str(sol_amount_lamports),
        "slippageBps": "80",
    }
    async with session.get(url, params=params, timeout=12) as resp:
        if resp.status == 200:
            data = await resp.json()
            if "outAmount" in data:
                usdc_out = int(data["outAmount"])
                return usdc_out / sol_amount_lamports * 1_000_000_000
    return None


async def execute_flash_arb(cex_price: float, dex_price: float):
    """Full atomic execution"""
    print(f"\n[{datetime.now()}] 🔥 TRIGGERED | CEX: ${cex_price:.2f} | DEX: ${dex_price:.2f}")

    spread_pct = (dex_price - cex_price) / cex_price * 100
    logger.info(
        "CEX-DEX Flash | Spread: %+.3f%% | Direction: %s",
        spread_pct,
        "DEX_CHEAP" if spread_pct > 0 else "CEX_CHEAP",
    )

    if spread_pct < MIN_SPREAD_PCT:
        logger.info("❌ Spread too small (< %s%%)", MIN_SPREAD_PCT)
        return

    client = AsyncClient(os.getenv("SOLANA_RPC_URL"))
    jup_executor = JupiterExecutor()
    keypair = jup_executor.keypair
    configure_jito(client, keypair)
    kamino = KaminoFlashLoan(client, keypair)

    # Calculate SOL to sell (convert USDC micro → lamports)
    sol_lamports = max(1, int(FLASH_AMOUNT_USDC * 1_000 * 0.96 / cex_price))  # conservative

    async with aiohttp.ClientSession() as session:
        quote = await get_jupiter_quote(session, sol_lamports)

    if not quote:
        await client.close()
        return

    opportunity = {
        "type": "cex_dex_flash_loan",
        "cex_price": cex_price,
        "dex_price": dex_price,
        "spread_pct": spread_pct,
        "flash_usdc": FLASH_AMOUNT_USDC / 1_000_000,
    }

    balance = (await client.get_balance(keypair.pubkey())).value
    decision = await ai_agent_decide(opportunity, balance)
    td = decision.get("trade_decision") if isinstance(decision.get("trade_decision"), dict) else {}
    logger.info(
        "AI Decision: %s | conf=%s",
        decision.get("final_action"),
        td.get("confidence"),
    )

    if decision.get("final_action") != "APPROVE":
        print("🛑 AI REJECTED")
        await client.close()
        return

    print("✅ AI APPROVED → Building tx...")

    try:
        # Build transaction
        borrow_ix = await kamino.get_flash_borrow_ix(str(USDC_MINT), FLASH_AMOUNT_USDC)
        swap_ixs = await jup_executor.get_swap_instructions(quote)
        fee = kamino._estimate_flash_loan_fee(FLASH_AMOUNT_USDC)
        repay_ix = await kamino.get_flash_repay_ix(str(USDC_MINT), FLASH_AMOUNT_USDC + fee)

        all_ixs = [borrow_ix] + swap_ixs + [repay_ix]

        recent = (await client.get_latest_blockhash()).value.blockhash
        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=all_ixs,
            address_lookup_table_accounts=[],
            recent_blockhash=recent,
        )
        tx = VersionedTransaction(msg, [keypair])

        # Simulate
        sim = await client.simulate_transaction(tx)
        if sim.value.err:
            print("❌ Sim failed")
            await client.close()
            return

        print("✅ Simulation OK")

        if TEST_MODE:
            print("🧪 TEST MODE - Not sending")
        else:
            wallet_safety.record_successful_simulation()
            ok, msg = wallet_safety.before_live_send(FLASH_AMOUNT_USDC)
            if ok:
                bundle_id = await send_jito_bundle(
                    [tx], client=client, keypair=keypair, tip_lamports=85000
                )
                if bundle_id:
                    print(f"🚀 BUNDLE SENT → {bundle_id}")
                    wallet_safety.record_live_trade_usdc_micro(FLASH_AMOUNT_USDC)
    finally:
        await client.close()


async def get_jupiter_quote(session, sol_lamports: int):
    url = os.getenv("JUPITER_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote")
    params = {
        "inputMint": str(SOL_MINT),
        "outputMint": str(USDC_MINT),
        "amount": str(sol_lamports),
        "slippageBps": "80",
    }
    async with session.get(url, params=params) as resp:
        return await resp.json() if resp.status == 200 else None


async def main():
    print(
        f"🚀 CEX-DEX Flash Loan Bot Started | Flash: {FLASH_AMOUNT_USDC / 1_000_000} USDC | Min Spread: {MIN_SPREAD_PCT}%"
    )

    while True:
        try:
            cex_price = await get_cex_price()
            if not cex_price:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            async with aiohttp.ClientSession() as session:
                dex_price = await get_jupiter_effective_price(session, 1_000_000_000)  # 1 SOL

            if not dex_price:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            spread = (dex_price - cex_price) / cex_price * 100

            if spread >= MIN_SPREAD_PCT:
                await execute_flash_arb(cex_price, dex_price)
            else:
                print(f"[{datetime.now()}] Spread: {spread:.3f}% → Waiting...")

        except Exception as e:
            print(f"Loop error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    asyncio.run(main())
