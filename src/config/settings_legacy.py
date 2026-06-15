# config.py
import os

from dotenv import load_dotenv

load_dotenv()

# ====================== RPC & WALLET ======================
RPC_URL = os.getenv("SOLANA_RPC_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")  # Base58 format

# ====================== KEY TOKENS ======================
# Base mint addresses (stable + SOL)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # Most liquid stable
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"  # Second stable
SOL = "So11111111111111111111111111111111111111112"  # Wrapped SOL (WSOL)

# Optional extra stables / bases (uncomment if you need them)
# EURC = "HzwqbK7q4K8y8nZ4j3Z9Z3Z9Z3Z9Z3Z9Z3Z9Z3Z9Z3Z"   # Euro stable (less common)
# PYUSD = "2b1kV6Dk8zU2dJ9qG4K5vL9pQ7rT8uV9wX0yZ1aB2cD" # PayPal USD (if liquidity grows)

# ====================== BOT SETTINGS ======================
MIN_PROFIT_PERCENT = 0.08  # Env MIN_PROFIT_PERCENT overrides; lower for backrun sensitivity
MAX_SLIPPAGE_BPS = 80  # 0.8% max slippage (adjust based on token volatility)
MAX_FLASH_LOAN_AMOUNT = 100_000_000_000  # e.g. 100,000 USDC (in smallest units)

# Priority fees (important for landing txs on Solana)
PRIORITY_FEE_LAMPORTS = 5000  # Micro-lamports per compute unit
JITO_TIP_LAMPORTS = (
    50_000  # ~0.00005 SOL; use JITO_TIP_LAMPORTS_MIN/MAX in jito_helper for random band
)

# Monitoring & Performance
WATCHLIST_SIZE = 150  # Legacy default (unused by main bot loop)
POLL_INTERVAL_SECONDS = 3  # How often to check for opportunities
MAX_CONCURRENT_CHECKS = 20  # Async concurrency limit

# Risk Management
MAX_POSITION_USD = 5000  # Max $ per trade (start very low!)
COOLDOWN_SECONDS = 30  # Cooldown after a trade
BLACKLIST_TOKENS = set()  # Add rug-prone mints here later

# Logging
LOG_TO_FILE = True
