"""Anti-rug validator for meme sniping."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.strategies.meme_sniping.config import meme_sniping_settings

logger = logging.getLogger(__name__)


async def _rpc_json(method: str, params: list[Any]) -> Any:
    from src.core.rpc_config import call_with_rpc_fallback

    async def _call(rpc_url: str) -> Any:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("error"):
                raise RuntimeError(payload["error"])
            return payload.get("result")

    return await call_with_rpc_fallback("default", _call, label=f"rpc:{method}")


async def check_dev_wallet_percentage(token_mint: str, coin: dict[str, Any]) -> float:
    """Return largest-holder concentration % (0-100)."""
    raw = coin.get("dev_percentage")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass

    try:
        result = await _rpc_json(
            "getTokenLargestAccounts",
            [token_mint, {"commitment": "confirmed"}],
        )
        accounts = (result or {}).get("value") or []
        if not accounts:
            return 100.0
        supply = await _rpc_json(
            "getTokenSupply",
            [token_mint, {"commitment": "confirmed"}],
        )
        total = float(((supply or {}).get("value") or {}).get("uiAmount") or 0)
        if total <= 0:
            return 100.0
        largest = float((accounts[0].get("uiAmount") or 0))
        return (largest / total) * 100.0
    except Exception as exc:
        logger.debug("dev_wallet check failed | mint=%s err=%s", token_mint[:12], exc)
        return float(coin.get("dev_percentage") or 50.0)


async def is_lp_burned(token_mint: str, coin: dict[str, Any]) -> bool:
    """Best-effort LP lock / graduation check."""
    if coin.get("lp_burned") is True:
        return True
    labels = coin.get("pair_labels") or []
    if any(str(l).lower() in ("lp burned", "locked", "burned") for l in labels):
        return True
    liq = float(coin.get("liquidity") or 0)
    # Graduated pump tokens on Raydium with deep liquidity are treated as LP-safe.
    if liq >= meme_sniping_settings.min_liquidity_usd * 1.5:
        return True
    if str(coin.get("source") or "") == "pump.fun" and coin.get("complete"):
        return True
    return False


async def get_holder_count(token_mint: str, coin: dict[str, Any]) -> int:
    """Holder estimate: RPC largest accounts + DexScreener activity proxy."""
    proxy = int(coin.get("holder_proxy") or 0)
    if proxy > 0:
        return proxy

    txns = coin.get("txns_h24") or {}
    try:
        buys = int(txns.get("buys") or 0)
        sells = int(txns.get("sells") or 0)
        activity_proxy = max(0, (buys + sells) * 3)
    except (TypeError, ValueError):
        activity_proxy = 0

    try:
        result = await _rpc_json(
            "getTokenLargestAccounts",
            [token_mint, {"commitment": "confirmed"}],
        )
        accounts = (result or {}).get("value") or []
        # Each returned account implies more holders exist beyond top-N.
        rpc_proxy = len(accounts) * 25
        return max(activity_proxy, rpc_proxy)
    except Exception:
        return activity_proxy


async def check_token_tax(token_mint: str, size_sol: float) -> float:
    """Estimate round-trip tax/slippage % via Jupiter buy+sell quote delta."""
    if size_sol <= 0:
        return 0.0
    try:
        from src.config.settings import get_settings
        from src.dex.jupiter import JupiterClient

        settings = get_settings()
        jup = JupiterClient(settings)
        lamports = int(size_sol * 1_000_000_000)
        buy = await jup.get_quote_for_mints(
            input_mint=str(jup.SOL),
            output_mint=token_mint,
            amount=lamports,
            slippage_bps=85,
        )
        if not buy:
            return 0.0
        out_amt = int(buy.get("outAmount") or buy.get("out_amount") or 0)
        if out_amt <= 0:
            return 0.0
        sell = await jup.get_quote_for_mints(
            input_mint=token_mint,
            output_mint=str(jup.SOL),
            amount=out_amt,
            slippage_bps=85,
        )
        if not sell:
            return 0.0
        back_lamports = int(sell.get("outAmount") or sell.get("out_amount") or 0)
        if back_lamports <= 0:
            return 100.0
        loss_pct = max(0.0, (1.0 - (back_lamports / lamports)) * 100.0)
        return loss_pct
    except Exception as exc:
        logger.debug("token tax check failed | mint=%s err=%s", token_mint[:12], exc)
        return 0.0


async def validate_token(token_mint: str, coin: dict[str, Any]) -> dict[str, Any]:
    """Multi-layer token safety check."""
    cfg = meme_sniping_settings
    pool_info = {
        "liquidity_usd": float(coin.get("liquidity") or 0),
        **coin,
    }

    dev_pct = await check_dev_wallet_percentage(token_mint, coin)
    lp_ok = await is_lp_burned(token_mint, coin) if cfg.require_lp_burned else True
    holders = await get_holder_count(token_mint, coin)
    tax_pct = await check_token_tax(token_mint, min(0.05, cfg.max_trade_sol * 0.1))

    checks: dict[str, bool] = {
        "liquidity": pool_info["liquidity_usd"] >= cfg.min_liquidity_usd,
        "dev_wallet": dev_pct <= cfg.max_dev_wallet_pct,
        "lp_burned": lp_ok,
        "holders": holders >= cfg.min_holder_count,
        "blacklisted": token_mint not in cfg.blacklist_tokens,
        "tax": tax_pct <= cfg.max_sell_tax_pct,
    }

    score = sum(checks.values()) / len(checks) * 100.0
    failed = [k for k, ok in checks.items() if not ok]
    passed = score >= cfg.validator_min_safety_score

    logger.info(
        "meme_sniping_validator | mint=%s passed=%s safety=%.1f failed=%s dev=%.1f holders=%d tax=%.2f%%",
        token_mint[:12],
        passed,
        score,
        failed,
        dev_pct,
        holders,
        tax_pct,
    )

    return {
        "passed": passed,
        "safety_score": score,
        "failed_checks": failed,
        "dev_wallet_pct": dev_pct,
        "holder_count": holders,
        "sell_tax_pct": tax_pct,
        "checks": checks,
    }
