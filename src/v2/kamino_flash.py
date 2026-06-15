"""v2.4 optional Kamino USDC flash → Jupiter buy SOL (reuses v1 builders)."""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from solana.rpc.async_api import AsyncClient
from src.dex.jupiter import send_signed_swap_transaction
from src.strategies.cex_dex import build_cex_dex_flash_tx, simulate
from src.strategies.dex_cex_reverse import DexCexReverseStrategy
from src.v2.config import V2Config

logger = logging.getLogger(__name__)


async def execute_v2_kamino_flash(
    reverse: DexCexReverseStrategy,
    opportunity: dict[str, Any],
    cfg: V2Config,
) -> dict[str, Any]:
    """
    Kamino borrow USDC → Jupiter USDC→SOL → repay → Backpack sell.

    Requires Ledger/hot signing on Jupiter executor and Klend reserve env pubkeys.
    """
    jupiter = reverse.jupiter
    settings = reverse.settings

    if settings.test_mode or settings.simulate:
        logger.info("KAMINO_FLASH skipped (test/simulate mode)")
        return {"status": "kamino_simulate_skip"}

    if not settings.live_trading_confirm_enabled:
        return {"status": "live_confirm_off"}

    if not await jupiter.has_signing():
        return {"status": "signing_unavailable"}

    size_micro = int(
        opportunity.get("size_usdc_micro") or cfg.kamino_flash_amount_usdc_micro
    )
    size_micro = min(size_micro, cfg.kamino_flash_amount_usdc_micro)
    if size_micro < 1_000_000:
        return {"status": "kamino_size_too_small"}

    cex_bid = float(opportunity.get("cex_bid") or 0)
    jup_price = float(opportunity.get("jup_price") or cex_bid)
    if cex_bid <= 0:
        return {"status": "no_cex_bid"}

    slippage = int(cfg.kamino_flash_repay_slippage_bps or cfg.execution_slippage_bps)
    os.environ.setdefault("KAMINO_MARKET_PUBKEY", cfg.kamino_lending_market)
    os.environ.setdefault("KAMINO_LENDING_MARKET_PUBKEY", cfg.kamino_lending_market)
    usdc_reserve = (os.getenv("KAMINO_USDC_RESERVE") or "").strip()
    if usdc_reserve:
        os.environ.setdefault("KAMINO_USDC_RESERVE", usdc_reserve)
        os.environ.setdefault("KAMINO_FLASH_RESERVE_PUBKEY", usdc_reserve)
    rpc = (
        settings.solana_rpc_url_fast
        or settings.solana_rpc_url
        or os.getenv("SOLANA_RPC_URL", "")
    )

    try:
        async with AsyncClient(rpc) as client:
            tx = await build_cex_dex_flash_tx(
                cex_bid,
                jup_price,
                size_micro,
                client=client,
                keypair=jupiter.keypair,
                jupiter=jupiter,
                direction="dex_cheap",
                slippage_bps=slippage,
            )
            if tx is None:
                return {"status": "kamino_build_failed"}

            if not await simulate(client, tx):
                return {"status": "kamino_sim_failed"}

            signed = await jupiter._sign_versioned(tx)
            signed_b64 = base64.b64encode(bytes(signed)).decode()
            from src.core.jito_tip import (
                expected_profit_lamports,
                get_dynamic_jito_tip,
            )

            net_bps = float(opportunity.get("roundtrip_net_bps") or opportunity.get("net_bps") or 0)
            profit_lam = expected_profit_lamports(net_bps, size_micro)
            tip = await get_dynamic_jito_tip(profit_lam)
            tx_result = await send_signed_swap_transaction(
                signed_b64,
                tip_lamports=tip,
                keypair=jupiter.keypair,
            )
            if not tx_result.get("success"):
                return {
                    "status": "kamino_bundle_failed",
                    "error": tx_result.get("error"),
                }

            tx_sig = str(tx_result.get("txid") or tx_result.get("bundle_id") or "")
            logger.info(
                "KAMINO_FLASH_SUCCESS | size_usdc=%.2f tx=%s",
                size_micro / 1_000_000.0,
                tx_sig,
            )

        buffer_sec = float(os.getenv("DEX_CEX_REVERSE_CEX_BUFFER_SEC", "6"))
        import asyncio

        await asyncio.sleep(buffer_sec)

        return await reverse.complete_cex_sell_after_buy(
            opportunity,
            size_usdc_micro=size_micro,
            buy_result={
                "success": True,
                "tx_sig": tx_sig,
                "sol_received": _estimate_sol_from_quote(size_micro, jup_price),
            },
        )
    except Exception as exc:
        logger.error("Kamino flash failed: %s", exc, exc_info=True)
        return {"status": "kamino_flash_error", "error": str(exc)}


def _estimate_sol_from_quote(size_micro: int, jup_price: float) -> float:
    if jup_price <= 0:
        return 0.0
    return (size_micro / 1_000_000.0) / jup_price * 0.995
