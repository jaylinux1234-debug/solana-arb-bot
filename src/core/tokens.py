# tokens.py — mint registry for CEX-DEX + Jupiter quotes
from __future__ import annotations

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SOL = "So11111111111111111111111111111111111111112"

BASE_TOKENS = {
    "USDC": USDC,
    "USDT": USDT,
    "SOL": SOL,
}

# Midcaps: verify mints on https://explorer.solana.com before size-up
COMMON_TOKENS: dict[str, str] = {
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNdVYx9",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
    "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQxDzNLp9",
    "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "W": "85VBFQZC9TZkfaptBzuSRmC6FfQyHUgsfPZ6tpLZ4r",
    "DRIFT": "DriFtupJYLTosbwoN8koQe4uRaKdXZnhMkNqR8z33",
    "GMT": "7i5KKsX2wei5ryFxfW85qCNgkpTWqdHwmMv9BDMVtEU",
    "MEW": "MEW1gQWJ3nEXg2qgRtVhrXb3bBp8cBNs8B8V2yH1A",
    "SAMO": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuGuios",
    "FIDA": "EchesyfXePKdLts4kD8opHjAfhZ4pybYp9C3xLuLWX",
    "RENDER": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof",
}

TOKEN_DECIMALS: dict[str, int] = {
    "SOL": 9,
    "BONK": 5,
    "JUP": 6,
    "WIF": 6,
    "PYTH": 6,
    "RAY": 6,
    "JTO": 9,
    "ORCA": 6,
    "POPCAT": 9,
    "W": 6,
    "DRIFT": 6,
    "GMT": 9,
    "MEW": 5,
    "SAMO": 9,
    "FIDA": 6,
    "RENDER": 8,
}


def get_token_mint(symbol: str) -> str | None:
    sym = (symbol or "").strip().upper()
    return BASE_TOKENS.get(sym) or COMMON_TOKENS.get(sym)


def get_mint(symbol: str) -> str | None:
    """Legacy alias."""
    return get_token_mint(symbol)


def is_base_token(mint: str) -> bool:
    return mint in BASE_TOKENS.values()
