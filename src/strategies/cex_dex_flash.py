#!/usr/bin/env python3
"""
cex_dex_flash_arb_fixed.py — Robust, production-ready CEX-DEX flash arbitrage.

Uses Kamino flash borrow → Jupiter swap (with ALTs) → repay via build_collateral_swap_tx.
"""

from __future__ import annotations

import asyncio
import logging
import os

from solana.rpc.async_api import AsyncClient

import src.core.wallet as wallet_safety
from src.core.circuit_breaker import circuit_breaker
from src.dex.jupiter import JupiterExecutor
from src.execution.jito import await_jito_bundle_poll, send_jito_bundle
from src.strategies.cex_dex_core import analyze_cex_dex_spread, resolve_direction

logger = logging.getLogger(__name__)

USDC_MINT_STR = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT_STR = "So11111111111111111111111111111111111111112"


def _test_mode() -> bool:
    return os.getenv("TEST_MODE", "true").lower() in ("1", "true", "yes")


def _signing_key_material() -> str:
    if (os.getenv("BOT_SIGNING_STRATEGY") or "").strip() == "cex_dex":
        return (os.getenv("PRIVATE_KEY_CEX_DEX") or os.getenv("PRIVATE_KEY") or "").strip()
    return (os.getenv("PRIVATE_KEY") or "").strip()


async def _simulate_with_retries(
    client: AsyncClient,
    tx,
    *,
    attempts: int = 3,
    delay_sec: float = 0.4,
) -> bool:
    for attempt in range(attempts):
        sim = await client.simulate_transaction(tx)
        if sim.value.err is None:
            logger.info("Simulation successful (attempt %s)", attempt + 1)
            return True
        logger.warning("Sim failed (attempt %s): %s", attempt + 1, sim.value.err)
        if attempt + 1 < attempts:
            await asyncio.sleep(delay_sec)
    return False


async def execute_flash_arb_fixed(
    cex_price: float,
    dex_price: float,
    direction: str | None = None,
    size_usdc_micro: int | None = None,
    jupiter: JupiterExecutor | None = None,
) -> str | None:
    """Main entry point for CEX-DEX flash arbitrage."""

    if circuit_breaker.should_pause():
        logger.warning("Flash arb blocked by circuit breaker")
        return None

    analysis = analyze_cex_dex_spread(cex_price, dex_price)
    if not analysis:
        logger.info("Invalid CEX/DEX prices")
        return None

    direction = resolve_direction(direction, cex_price, dex_price)
    if not direction:
        return None

    flash_usdc = size_usdc_micro or int(os.getenv("CEX_DEX_FLASH_AMOUNT_USDC_MICRO", "35000000"))
    slippage_bps = int(
        os.getenv("CEX_DEX_STRATEGY_SLIPPAGE_BPS", os.getenv("JUPITER_SLIPPAGE_BPS", "80"))
    )

    own_client = jupiter is None
    client: AsyncClient
    if own_client:
        rpc = os.getenv("SOLANA_RPC_URL")
        if not rpc:
            logger.error("SOLANA_RPC_URL unset")
            return None
        client = AsyncClient(rpc)
        pk = _signing_key_material()
        if not pk:
            logger.error("No signing key in env")
            await client.close()
            return None
        jupiter = JupiterExecutor(pk)
    else:
        client = jupiter.client

    try:
        if direction == "dex_cheap":
            flash_amount = flash_usdc
            borrow_mint = USDC_MINT_STR
            target_mint = SOL_MINT_STR
        else:
            flash_amount = max(1, int(flash_usdc * 1_000 / cex_price * 0.965))
            borrow_mint = SOL_MINT_STR
            target_mint = USDC_MINT_STR

        tx = await jupiter.build_collateral_swap_tx(
            borrow_reserve_mint=borrow_mint,
            target_collateral_mint=target_mint,
            flash_amount=flash_amount,
            slippage_bps=slippage_bps,
        )

        sim_attempts = max(1, int(os.getenv("CEX_DEX_FLASH_SIM_ATTEMPTS", "3")))
        sim_delay = float(os.getenv("CEX_DEX_FLASH_SIM_RETRY_DELAY_SEC", "0.4"))
        if not await _simulate_with_retries(client, tx, attempts=sim_attempts, delay_sec=sim_delay):
            logger.error("Simulation failed after retries")
            return None

        wallet_safety.record_successful_simulation()

        if _test_mode():
            logger.info(
                "TEST_MODE: would send CEX-DEX flash | direction=%s flash_usdc_micro=%s",
                direction,
                flash_usdc,
            )
            return None

        ok, reason = wallet_safety.before_live_send(flash_usdc)
        if not ok:
            logger.warning("Wallet safety blocked trade: %s", reason)
            return None

        bundle_id = await send_jito_bundle(
            [tx],
            client=client,
            keypair=jupiter.keypair,
            tip_lamports=int(os.getenv("CEX_DEX_FLASH_JITO_TIP_LAMPORTS", "120000")),
        )

        if not bundle_id:
            return None

        wallet_safety.record_live_trade_usdc_micro(flash_usdc)
        logger.info(
            "CEX-DEX flash success | direction=%s | bundle=%s",
            direction,
            bundle_id,
        )

        if os.getenv("JITO_AWAIT_BUNDLE_POLL", "true").lower() in ("1", "true", "yes"):
            await await_jito_bundle_poll(bundle_id)

        return bundle_id

    except Exception:
        logger.exception("execute_flash_arb_fixed failed")
        return None
    finally:
        if own_client:
            await client.close()
