# src/strategies/cex_dex_flash_build.py
"""Kamino flash + Jupiter swap transaction build for CEX-DEX (used by ``CexDexCycle``)."""

from __future__ import annotations

import logging
import os

from solana.rpc.async_api import AsyncClient
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from src.config.settings import settings
from src.dex.jupiter import JupiterClient, JupiterExecutor
from src.dex.kamino import Kamino, KaminoFlashLoan
from src.strategies.cex_dex_core import resolve_direction

logger = logging.getLogger(__name__)

USDC_MINT_STR = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT_STR = "So11111111111111111111111111111111111111112"


async def simulate(client: AsyncClient, tx: VersionedTransaction) -> bool:
    sim = await client.simulate_transaction(tx)
    if sim.value.err is None:
        return True
    logger.warning("CEX-DEX flash simulation failed: %s", sim.value.err)
    return False


async def build_versioned_tx(
    jupiter: JupiterClient,
    instructions: list[Instruction],
    *,
    alt_addresses: list[str] | None = None,
) -> VersionedTransaction:
    return await jupiter.build_flash_loan_tx(
        instructions,
        alt_address_strings=alt_addresses or [],
    )


def _jupiter_swap_instructions(
    kamino: KaminoFlashLoan,
    payload: dict,
) -> list[Instruction]:
    if payload.get("error"):
        raise ValueError(f"Jupiter swap-instructions error: {payload.get('error')}")
    if not payload.get("swapInstruction"):
        raise ValueError("Jupiter swap-instructions missing swapInstruction")
    out: list[Instruction] = []
    for cb in payload.get("computeBudgetInstructions") or []:
        out.append(kamino._convert_jupiter_ix(cb))
    for setup in payload.get("setupInstructions") or []:
        out.append(kamino._convert_jupiter_ix(setup))
    out.append(kamino._convert_jupiter_ix(payload["swapInstruction"]))
    if cleanup := payload.get("cleanupInstruction"):
        out.append(kamino._convert_jupiter_ix(cleanup))
    return out


async def build_cex_dex_flash_tx(
    cex_mid: float,
    dex_mid: float,
    size_usdc: int,
    *,
    client: AsyncClient,
    keypair: Keypair,
    jupiter: JupiterClient | JupiterExecutor | None = None,
    direction: str | None = None,
    slippage_bps: int | None = None,
) -> VersionedTransaction | None:
    """Kamino flash borrow → Jupiter swap → flash repay."""
    direction = resolve_direction(direction, cex_mid, dex_mid) or (
        "dex_cheap" if dex_mid > cex_mid else "cex_cheap"
    )
    user = keypair.pubkey()
    bps = (
        slippage_bps
        if slippage_bps is not None
        else int(os.getenv("CEX_DEX_FLASH_QUOTE_SLIPPAGE_BPS", str(settings.max_slippage_bps)))
    )

    own_jupiter = jupiter is None
    if own_jupiter:
        jupiter = JupiterExecutor()

    kamino_helper = KaminoFlashLoan(client, keypair)

    try:
        if direction == "dex_cheap":
            borrow_mint = USDC_MINT_STR
            flash_amount = size_usdc
            quote_in, quote_out = USDC_MINT_STR, SOL_MINT_STR
        else:
            borrow_mint = SOL_MINT_STR
            flash_amount = max(1, int(size_usdc * 1_000 / cex_mid * 0.965))
            quote_in, quote_out = SOL_MINT_STR, USDC_MINT_STR

        borrow_ix = Kamino.get_flash_borrow_ix(borrow_mint, flash_amount, user)

        quote = await jupiter.get_jupiter_quote(quote_in, quote_out, flash_amount, slippage_bps=bps)
        if not quote or quote.get("error"):
            logger.error("Jupiter quote failed for CEX-DEX flash")
            return None

        payload = await jupiter.get_swap_instructions(quote, slippage_bps=bps)
        swap_ixs = _jupiter_swap_instructions(kamino_helper, payload)

        fee = kamino_helper._estimate_flash_loan_fee(flash_amount)
        repay_ix = Kamino.get_flash_repay_ix(
            borrow_mint,
            flash_amount + fee,
            user,
            borrow_ix_index=0,
        )

        alt_addrs = list(payload.get("addressLookupTableAddresses") or [])
        return await build_versioned_tx(
            jupiter,
            [borrow_ix, *swap_ixs, repay_ix],
            alt_addresses=alt_addrs,
        )
    except Exception:
        logger.exception("build_cex_dex_flash_tx failed")
        return None
    finally:
        if own_jupiter:
            await jupiter.client.close()
