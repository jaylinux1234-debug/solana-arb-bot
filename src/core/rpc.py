import asyncio
import os

# Use AsyncClient for bots (much better performance)
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey

from src.config.settings import settings
from src.core.security import secure_load_keypair, validate_bot_environment

# ====================== CONFIG ======================
RPC_URL = settings.SOLANA_RPC_URL
PRIVATE_KEY = settings.active_private_key

if not RPC_URL or not PRIVATE_KEY:
    raise ValueError("Missing SOLANA_RPC_URL or PRIVATE_KEY in .env file")

_test_mode = os.getenv("TEST_MODE", "true").lower() == "true"
validate_bot_environment(rpc_url=RPC_URL, private_key=PRIVATE_KEY, test_mode=_test_mode)

# Load wallet (supports formats handled by secure_load_keypair)
keypair = secure_load_keypair(PRIVATE_KEY)
pubkey: Pubkey = keypair.pubkey()

# Global async client (reuse it)
client = AsyncClient(RPC_URL, commitment=Confirmed)


async def get_balance():
    """Get SOL balance in SOL (not lamports)"""
    response = await client.get_balance(pubkey)
    balance_lamports = response.value
    balance_sol = balance_lamports / 1_000_000_000
    print(f"Wallet: {pubkey}")
    print(f"Balance: {balance_sol:.4f} SOL")
    return balance_sol


async def check_connection():
    """Test RPC connection"""
    connected = await client.is_connected()
    print(f"RPC Connected: {connected} -> {RPC_URL}")
    return connected


async def close_connection():
    await client.close()


async def main():
    await check_connection()
    await get_balance()
    await close_connection()


# ====================== RUN TEST ======================
if __name__ == "__main__":
    asyncio.run(main())
