"""Jupiter quote/swap parameter builder (env-driven route quality)."""

from __future__ import annotations

import os
from typing import Any

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def is_sol_usdc_pair(input_mint: str, output_mint: str) -> bool:
    pair = {input_mint, output_mint}
    return SOL_MINT in pair and USDC_MINT in pair


def resolve_slippage_bps(
    input_mint: str,
    output_mint: str,
    *,
    override: int | None = None,
) -> int:
    """Tighter slippage on SOL/USDC (default 40 bps); else ``MAX_SLIPPAGE_BPS``."""
    if override is not None:
        return int(override)
    if is_sol_usdc_pair(input_mint, output_mint):
        return int(os.getenv("JUPITER_SOL_USDC_SLIPPAGE_BPS", "40"))
    return int(os.getenv("MAX_SLIPPAGE_BPS", "100"))


def build_quote_query_params(
    amount: int,
    *,
    input_mint: str,
    output_mint: str,
    slippage_bps: int | None = None,
    platform_fee_bps: int = 0,
) -> dict[str, str]:
    """
    Query params for Jupiter quote API.

    See https://station.jup.ag/docs/apis/swap-api/quote
    """
    bps = slippage_bps if slippage_bps is not None else resolve_slippage_bps(
        input_mint, output_mint
    )
    params: dict[str, str] = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(bps),
        "platformFeeBps": str(platform_fee_bps),
    }

    if _env_bool("JUPITER_RESTRICT_INTERMEDIATE_TOKENS", True):
        params["restrictIntermediateTokens"] = "true"

    if is_sol_usdc_pair(input_mint, output_mint) and _env_bool(
        "JUPITER_ONLY_DIRECT_ROUTES", True
    ):
        params["onlyDirectRoutes"] = "true"

    max_accounts = (os.getenv("JUPITER_MAX_ACCOUNTS") or "").strip()
    if max_accounts:
        params["maxAccounts"] = max_accounts

    dexes = (os.getenv("JUPITER_PREFERRED_DEXES") or "").strip()
    if not dexes and _env_bool("ENABLE_MULTI_DEX_ROUTES", False):
        dexes = (os.getenv("MULTI_DEX_PREFERRED") or "Phoenix,Raydium,Orca").strip()
    if dexes:
        params["dexes"] = dexes

    exclude_dexes = (os.getenv("JUPITER_EXCLUDE_DEXES") or "").strip()
    if exclude_dexes:
        params["excludeDexes"] = exclude_dexes

    return params


def build_swap_request_body(
    raw_quote: dict[str, Any],
    user_pubkey: str,
    *,
    slippage_bps: int | None = None,
) -> dict[str, Any]:
    """POST body for Jupiter swap transaction build."""
    in_mint = str(raw_quote.get("inputMint", ""))
    out_mint = str(raw_quote.get("outputMint", ""))
    bps = slippage_bps if slippage_bps is not None else resolve_slippage_bps(
        in_mint, out_mint
    )
    payload: dict[str, Any] = {
        "quoteResponse": raw_quote,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "skipUserAccountsRpcCalls": True,
        "slippageBps": bps,
    }
    if _env_bool("JUPITER_DYNAMIC_SLIPPAGE", True):
        payload["dynamicSlippage"] = True
    if _env_bool("JUPITER_LEDGER_COMPAT", True):
        payload["useSharedAccounts"] = False
        if _env_bool("JUPITER_AS_LEGACY_TRANSACTION", True):
            payload["asLegacyTransaction"] = True
    elif _env_bool("JUPITER_USE_SHARED_ACCOUNTS", True):
        payload["useSharedAccounts"] = True
    cu_price = (os.getenv("JUPITER_COMPUTE_UNIT_PRICE_MICROLAMPORTS") or "").strip()
    if cu_price:
        try:
            payload["computeUnitPriceMicroLamports"] = int(cu_price)
        except ValueError:
            pass
    return payload


def quote_route_hops(raw_quote: dict[str, Any] | None) -> int:
    """Count route legs from ``routePlan`` (for logging / near-miss context)."""
    if not raw_quote:
        return 0
    plan = raw_quote.get("routePlan") or []
    return len(plan) if isinstance(plan, list) else 0
