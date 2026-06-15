# src/dex/kamino.py — Klend flash loan + Jupiter swap composition.
#
# Reserve pubkeys (Kamino UI / Solscan): KAMINO_USDC_RESERVE, KAMINO_SOL_RESERVE,
# KAMINO_RESERVE_<mint>, or KAMINO_FLASH_RESERVE_<mint>.
# Market: KAMINO_MARKET_PUBKEY or KAMINO_LENDING_MARKET_PUBKEY (defaults to main market).
# KLend IDL (anchorpy Program): idls/klend.json via src.dex.klend_program.get_klend_program.

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import aiohttp
import httpx
from solana.rpc.async_api import AsyncClient
from solders.address_lookup_table_account import AddressLookupTable, AddressLookupTableAccount
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.sysvar import INSTRUCTIONS
from solders.transaction import VersionedTransaction
from spl.token.instructions import get_associated_token_address

from src.config.settings import settings

logger = logging.getLogger(__name__)

KAMINO_PROGRAM_ID = Pubkey.from_string("KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD")
_DEFAULT_KAMINO_MARKET_PUBKEY = (
    getattr(settings, "KAMINO_MARKET_PUBKEY", None)
    or getattr(settings, "KAMINO_LENDING_MARKET_PUBKEY", None)
    or os.getenv("KAMINO_LENDING_MARKET_PUBKEY")
    or os.getenv("KAMINO_MARKET_PUBKEY")
    or "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF"
)
MAIN_MARKET = Pubkey.from_string(_DEFAULT_KAMINO_MARKET_PUBKEY)
KAMINO_MAIN_MARKET = MAIN_MARKET

# Anchor ix discriminators (@kamino-finance/klend-sdk idl_codegen)
FLASH_BORROW_DISCRIMINATOR = bytes([135, 231, 52, 167, 7, 52, 212, 193])
FLASH_REPAY_DISCRIMINATOR = bytes([185, 117, 0, 203, 96, 245, 180, 186])
LIQUIDATE_OBLIGATION_DISCRIMINATOR = bytes.fromhex("b1479abce2854a37")
SEED_LENDING_MARKET_AUTH = b"lma"
SEED_RESERVE_LIQ_SUPPLY = b"reserve_liq_supply"
SEED_RESERVE_COLL_MINT = b"reserve_coll_mint"
SEED_RESERVE_COLL_SUPPLY = b"reserve_coll_supply"
SEED_FEE_RECEIVER = b"fee_receiver"
SEED_REFERRER_TOKEN_STATE = b"referrer_acc"
KAMINO_METRICS_URL_TMPL = "https://api.kamino.finance/kamino-market/{market}/reserves/metrics"
_reserve_liquidity_mint_cache: dict[tuple[str, str], str] = {}
_reserve_vault_cache: dict[str, tuple[str, str]] = {}
NULL_REFERRER = Pubkey.from_string("nu11111111111111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

USDC_MINT_STR = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT_STR = "So11111111111111111111111111111111111111112"

USDC_MINT = Pubkey.from_string(USDC_MINT_STR)
SOL_MINT = Pubkey.from_string(SOL_MINT_STR)

_U64_MAX = 2**64 - 1
KAMINO_API_BASE = "https://api.kamino.finance"


async def get_kamino_reserve_liquidity(
    market_pubkey: str = None,
    token_symbol: str = "USDC"
) -> Decimal:
    """
    Fetch available liquidity for flash loans from Kamino.
    Returns amount in USDC (full units, not micro).
    """
    if not market_pubkey:
        market_pubkey = getattr(settings, "KAMINO_LENDING_MARKET_PUBKEY", None)
        if not market_pubkey:
            market_pubkey = getattr(settings, "kamino_lending_market_pubkey", None)

    if not market_pubkey:
        logger.error("No Kamino market pubkey configured")
        return Decimal("1000000")

    url = f"{KAMINO_API_BASE}/kamino-market/{market_pubkey}/reserves/metrics"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        if isinstance(data, dict):
            reserves = data.get("reserves") or data.get("data") or []
        else:
            reserves = data

        for reserve in reserves:
            if str(reserve.get("liquidityToken", "")).upper() == token_symbol.upper():
                available = Decimal(str(reserve.get("availableLiquidity", 0)))
                total_borrowed = Decimal(str(reserve.get("totalBorrowUsd", 0)))
                logger.info(
                    f"Kamino {token_symbol} liquidity: {available:,.0f} | borrowed: {total_borrowed:,.0f}"
                )
                return available

        logger.warning(f"No {token_symbol} reserve found in Kamino market")
        return Decimal("0")

    except Exception as e:
        logger.error(f"Failed to fetch Kamino liquidity: {e}")
        return Decimal("1000000")  # safe fallback


_kamino_cache = {"liquidity": Decimal("0"), "ts": 0}


async def get_cached_kamino_liquidity() -> Decimal:
    """Cached version (refreshes every 12 seconds)"""
    import time

    now = time.time()
    if now - _kamino_cache["ts"] > 12:
        _kamino_cache["liquidity"] = await get_kamino_reserve_liquidity()
        _kamino_cache["ts"] = now
    return _kamino_cache["liquidity"]


def _flash_loan_amount_u64_le(amount: int) -> bytes:
    """Little-endian u64 payload for Klend flash borrow/repay amount args."""
    if not isinstance(amount, int):
        raise TypeError(f"flash loan amount must be int, got {type(amount).__name__}")
    if amount < 0:
        raise ValueError(f"flash loan amount must be non-negative, got {amount}")
    if amount > _U64_MAX:
        raise ValueError("flash loan amount exceeds u64 max")
    return amount.to_bytes(8, "little")


def _mint_str(mint: str | Pubkey) -> str:
    return str(mint) if isinstance(mint, Pubkey) else mint


class Kamino:
    """Static Klend flash borrow/repay instruction builders (IDL-aligned account layout)."""

    @staticmethod
    def lending_market() -> Pubkey:
        raw = (
            os.getenv("KAMINO_LENDING_MARKET_PUBKEY")
            or os.getenv("KAMINO_LENDING_MARKET")
            or os.getenv("KAMINO_MARKET_PUBKEY")
            or getattr(settings, "KAMINO_LENDING_MARKET_PUBKEY", None)
            or getattr(settings, "KAMINO_MARKET_PUBKEY", None)
            or ""
        ).strip()
        if raw:
            return Pubkey.from_string(raw)
        return MAIN_MARKET

    @staticmethod
    def reserve_for_mint(mint: str) -> Pubkey:
        m = mint.strip()
        env_map: dict[str, list[str | None]] = {
            USDC_MINT_STR: [
                os.getenv("KAMINO_USDC_RESERVE"),
                os.getenv("KAMINO_FLASH_RESERVE_PUBKEY"),
                os.getenv("KAMINO_USDC_DEBT_RESERVE"),
            ],
            SOL_MINT_STR: [
                os.getenv("KAMINO_SOL_RESERVE"),
                os.getenv("KAMINO_FLASH_SOL_RESERVE_PUBKEY"),
            ],
        }
        for candidate in env_map.get(m, []):
            if candidate and str(candidate).strip():
                return Pubkey.from_string(str(candidate).strip())
        for key in (f"KAMINO_RESERVE_{m}", f"KAMINO_FLASH_RESERVE_{m}"):
            custom = (os.getenv(key) or "").strip()
            if custom:
                return Pubkey.from_string(custom)
        raise ValueError(
            "No Klend reserve pubkey for mint. Set KAMINO_USDC_RESERVE / KAMINO_SOL_RESERVE, "
            f"KAMINO_RESERVE_{m}, or KAMINO_FLASH_RESERVE_{m}."
        )

    @staticmethod
    def _pda(seeds: list[bytes], program_id: Pubkey) -> Pubkey:
        return Pubkey.find_program_address(seeds, program_id)[0]

    @classmethod
    def _lending_market_authority(cls, lending_market: Pubkey) -> Pubkey:
        return cls._pda([SEED_LENDING_MARKET_AUTH, bytes(lending_market)], KAMINO_PROGRAM_ID)

    @classmethod
    def _reserve_liquidity_supply_vault(cls, reserve: Pubkey) -> Pubkey:
        return cls._pda([SEED_RESERVE_LIQ_SUPPLY, bytes(reserve)], KAMINO_PROGRAM_ID)

    @classmethod
    def _reserve_fee_receiver(cls, reserve: Pubkey) -> Pubkey:
        return cls._pda([SEED_FEE_RECEIVER, bytes(reserve)], KAMINO_PROGRAM_ID)

    @classmethod
    def _reserve_collateral_mint(cls, reserve: Pubkey) -> Pubkey:
        return cls._pda([SEED_RESERVE_COLL_MINT, bytes(reserve)], KAMINO_PROGRAM_ID)

    @classmethod
    def _reserve_collateral_supply(cls, reserve: Pubkey) -> Pubkey:
        return cls._pda([SEED_RESERVE_COLL_SUPPLY, bytes(reserve)], KAMINO_PROGRAM_ID)

    @classmethod
    def _referrer_token_state(cls, referrer: Pubkey, reserve: Pubkey) -> Pubkey:
        return cls._pda(
            [SEED_REFERRER_TOKEN_STATE, bytes(referrer), bytes(reserve)],
            KAMINO_PROGRAM_ID,
        )

    @staticmethod
    def _resolve_lending_market(lending_market_pubkey: Pubkey | str | None = None) -> Pubkey:
        if lending_market_pubkey:
            return Pubkey.from_string(str(lending_market_pubkey).strip())
        return Kamino.lending_market()

    @staticmethod
    async def fetch_reserve_liquidity_vaults(
        keypair: Keypair,
        reserve_pubkey: Pubkey | str,
    ) -> tuple[Pubkey, Pubkey]:
        """Read supply/fee vault pubkeys from on-chain Reserve (V2 reserves may not use PDAs)."""
        key = str(reserve_pubkey).strip()
        if key in _reserve_vault_cache:
            supply_s, fee_s = _reserve_vault_cache[key]
            return Pubkey.from_string(supply_s), Pubkey.from_string(fee_s)

        from src.core.rpc_config import call_with_rpc_fallback
        from src.dex.klend_program import get_klend_program

        reserve_pk = Pubkey.from_string(key)

        async def _fetch_reserve(rpc_url: str):
            async with AsyncClient(rpc_url) as client:
                program = await get_klend_program(
                    client, keypair, ephemeral=True
                )
                return await program.account["Reserve"].fetch(reserve_pk)

        acc = await call_with_rpc_fallback(
            "default", _fetch_reserve, label="kamino_reserve_vaults"
        )
        supply = Pubkey.from_string(str(acc.liquidity.supply_vault))
        fee = Pubkey.from_string(str(acc.liquidity.fee_vault))
        _reserve_vault_cache[key] = (str(supply), str(fee))
        return supply, fee

    @staticmethod
    def get_flash_borrow_ix(
        mint: str,
        amount: int,
        user: Pubkey,
        *,
        reserve_pubkey: Pubkey | str | None = None,
        lending_market_pubkey: Pubkey | str | None = None,
        supply_vault: Pubkey | str | None = None,
        fee_vault: Pubkey | str | None = None,
    ) -> Instruction:
        mint_pk = Pubkey.from_string(mint)
        lending_market = Kamino._resolve_lending_market(lending_market_pubkey)
        reserve = (
            Pubkey.from_string(str(reserve_pubkey).strip())
            if reserve_pubkey
            else Kamino.reserve_for_mint(mint)
        )
        lending_market_authority = Kamino._lending_market_authority(lending_market)
        reserve_source_liquidity = (
            Pubkey.from_string(str(supply_vault).strip())
            if supply_vault
            else Kamino._reserve_liquidity_supply_vault(reserve)
        )
        user_destination_liquidity = get_associated_token_address(user, mint_pk)
        reserve_liquidity_fee_receiver = (
            Pubkey.from_string(str(fee_vault).strip())
            if fee_vault
            else Kamino._reserve_fee_receiver(reserve)
        )
        # Optional referrer accounts: Anchor uses the program id when absent (klend-sdk none()).
        optional_referrer = AccountMeta(KAMINO_PROGRAM_ID, is_signer=False, is_writable=False)

        data = FLASH_BORROW_DISCRIMINATOR + _flash_loan_amount_u64_le(amount)
        accounts = [
            AccountMeta(user, is_signer=True, is_writable=False),
            AccountMeta(lending_market_authority, is_signer=False, is_writable=False),
            AccountMeta(lending_market, is_signer=False, is_writable=False),
            AccountMeta(reserve, is_signer=False, is_writable=True),
            AccountMeta(mint_pk, is_signer=False, is_writable=False),
            AccountMeta(reserve_source_liquidity, is_signer=False, is_writable=True),
            AccountMeta(user_destination_liquidity, is_signer=False, is_writable=True),
            AccountMeta(reserve_liquidity_fee_receiver, is_signer=False, is_writable=True),
            optional_referrer,
            optional_referrer,
            AccountMeta(INSTRUCTIONS, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        ]
        return Instruction(program_id=KAMINO_PROGRAM_ID, accounts=accounts, data=data)

    @staticmethod
    def get_flash_repay_ix(
        mint: str,
        amount: int,
        user: Pubkey,
        borrow_ix_index: int = 0,
        *,
        reserve_pubkey: Pubkey | str | None = None,
        lending_market_pubkey: Pubkey | str | None = None,
        supply_vault: Pubkey | str | None = None,
        fee_vault: Pubkey | str | None = None,
    ) -> Instruction:
        mint_pk = Pubkey.from_string(mint)
        lending_market = Kamino._resolve_lending_market(lending_market_pubkey)
        reserve = (
            Pubkey.from_string(str(reserve_pubkey).strip())
            if reserve_pubkey
            else Kamino.reserve_for_mint(mint)
        )
        lending_market_authority = Kamino._lending_market_authority(lending_market)
        reserve_liquidity_supply = (
            Pubkey.from_string(str(supply_vault).strip())
            if supply_vault
            else Kamino._reserve_liquidity_supply_vault(reserve)
        )
        user_source_liquidity = get_associated_token_address(user, mint_pk)
        reserve_liquidity_fee_receiver = (
            Pubkey.from_string(str(fee_vault).strip())
            if fee_vault
            else Kamino._reserve_fee_receiver(reserve)
        )
        optional_referrer = AccountMeta(KAMINO_PROGRAM_ID, is_signer=False, is_writable=False)

        try:
            idx = int(borrow_ix_index)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"borrow_ix_index must be int-compatible, got {borrow_ix_index!r}"
            ) from exc
        if idx < 0 or idx > 255:
            raise ValueError(f"borrow_ix_index must be 0..255, got {borrow_ix_index}")

        data = FLASH_REPAY_DISCRIMINATOR + _flash_loan_amount_u64_le(amount) + bytes([idx])
        accounts = [
            AccountMeta(user, is_signer=True, is_writable=False),
            AccountMeta(lending_market_authority, is_signer=False, is_writable=False),
            AccountMeta(lending_market, is_signer=False, is_writable=False),
            AccountMeta(reserve, is_signer=False, is_writable=True),
            AccountMeta(mint_pk, is_signer=False, is_writable=False),
            AccountMeta(reserve_liquidity_supply, is_signer=False, is_writable=True),
            AccountMeta(user_source_liquidity, is_signer=False, is_writable=True),
            AccountMeta(reserve_liquidity_fee_receiver, is_signer=False, is_writable=True),
            optional_referrer,
            optional_referrer,
            AccountMeta(INSTRUCTIONS, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        ]
        return Instruction(program_id=KAMINO_PROGRAM_ID, accounts=accounts, data=data)

    @staticmethod
    def get_liquidate_obligation_ix(
        *,
        liquidator: Pubkey,
        obligation: Pubkey,
        lending_market: Pubkey,
        repay_reserve: Pubkey,
        repay_liquidity_mint: Pubkey,
        withdraw_reserve: Pubkey,
        withdraw_liquidity_mint: Pubkey,
        liquidity_amount: int,
        min_acceptable_received_liquidity_amount: int,
        max_allowed_ltv_override_percent: int = 0,
    ) -> Instruction:
        """KLend liquidate_obligation_and_redeem_reserve_collateral (IDL account order)."""
        lending_market_authority = Kamino._lending_market_authority(lending_market)
        repay_reserve_liquidity_supply = Kamino._reserve_liquidity_supply_vault(repay_reserve)
        withdraw_reserve_collateral_mint = Kamino._reserve_collateral_mint(withdraw_reserve)
        withdraw_reserve_collateral_supply = Kamino._reserve_collateral_supply(withdraw_reserve)
        withdraw_reserve_liquidity_supply = Kamino._reserve_liquidity_supply_vault(withdraw_reserve)
        withdraw_reserve_liquidity_fee_receiver = Kamino._reserve_fee_receiver(withdraw_reserve)

        user_source_liquidity = get_associated_token_address(liquidator, repay_liquidity_mint)
        user_destination_collateral = get_associated_token_address(
            liquidator, withdraw_reserve_collateral_mint
        )
        user_destination_liquidity = get_associated_token_address(
            liquidator, withdraw_liquidity_mint
        )

        data = (
            LIQUIDATE_OBLIGATION_DISCRIMINATOR
            + int(liquidity_amount).to_bytes(8, "little")
            + int(min_acceptable_received_liquidity_amount).to_bytes(8, "little")
            + int(max_allowed_ltv_override_percent).to_bytes(8, "little")
        )
        accounts = [
            AccountMeta(liquidator, is_signer=True, is_writable=False),
            AccountMeta(obligation, is_signer=False, is_writable=True),
            AccountMeta(lending_market, is_signer=False, is_writable=False),
            AccountMeta(lending_market_authority, is_signer=False, is_writable=False),
            AccountMeta(repay_reserve, is_signer=False, is_writable=True),
            AccountMeta(repay_liquidity_mint, is_signer=False, is_writable=False),
            AccountMeta(repay_reserve_liquidity_supply, is_signer=False, is_writable=True),
            AccountMeta(withdraw_reserve, is_signer=False, is_writable=True),
            AccountMeta(withdraw_liquidity_mint, is_signer=False, is_writable=False),
            AccountMeta(withdraw_reserve_collateral_mint, is_signer=False, is_writable=True),
            AccountMeta(withdraw_reserve_collateral_supply, is_signer=False, is_writable=True),
            AccountMeta(withdraw_reserve_liquidity_supply, is_signer=False, is_writable=True),
            AccountMeta(
                withdraw_reserve_liquidity_fee_receiver, is_signer=False, is_writable=True
            ),
            AccountMeta(user_source_liquidity, is_signer=False, is_writable=True),
            AccountMeta(user_destination_collateral, is_signer=False, is_writable=True),
            AccountMeta(user_destination_liquidity, is_signer=False, is_writable=True),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(INSTRUCTIONS, is_signer=False, is_writable=False),
        ]
        return Instruction(program_id=KAMINO_PROGRAM_ID, accounts=accounts, data=data)


async def fetch_reserve_liquidity_mint(market_pk: str, reserve_pk: str) -> str:
    """Resolve reserve liquidity mint from Kamino metrics API (cached)."""
    key = (market_pk, reserve_pk)
    cached = _reserve_liquidity_mint_cache.get(key)
    if cached:
        return cached

    url = KAMINO_METRICS_URL_TMPL.format(market=market_pk)
    headers = {"User-Agent": "solana-arb-bot/1.0", "Accept": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                raise ValueError(
                    f"Kamino reserve metrics HTTP {resp.status} for market={market_pk[:8]}"
                )
            data = await resp.json()
    if not isinstance(data, list):
        raise ValueError(f"Kamino reserve metrics: expected list, got {type(data).__name__}")

    for row in data:
        if not isinstance(row, dict):
            continue
        if str(row.get("reserve") or "") != reserve_pk:
            continue
        mint = str(row.get("liquidityTokenMint") or "").strip()
        if not mint:
            break
        _reserve_liquidity_mint_cache[key] = mint
        return mint

    raise ValueError(f"No liquidityTokenMint for reserve={reserve_pk[:12]} market={market_pk[:8]}")


class KaminoFlashLoan:
    def __init__(self, client: AsyncClient, keypair: Keypair):
        self.client = client
        self.keypair = keypair
        self.user_pubkey = keypair.pubkey()

    async def get_flash_borrow_ix(
        self,
        mint: str | Pubkey,
        amount: int,
        obligation: Pubkey | None = None,
    ) -> Instruction:
        _ = obligation
        return await self._get_kamino_flash_borrow_ix(_mint_str(mint), amount)

    async def get_flash_repay_ix(
        self,
        mint: str | Pubkey,
        amount: int,
        obligation: Pubkey | None = None,
        *,
        borrow_instruction_index: int = 0,
    ) -> Instruction:
        _ = obligation
        return await self._get_kamino_flash_repay_ix(
            _mint_str(mint), amount, borrow_instruction_index=borrow_instruction_index
        )

    async def get_liquidation_ix(
        self,
        obligation: Pubkey,
        debt_reserve: Pubkey,
        collateral_reserve: Pubkey,
        debt_amt: int,
        *,
        lending_market: Pubkey | None = None,
        repay_mint: str | Pubkey | None = None,
        withdraw_liquidity_mint: str | Pubkey | None = None,
        liquidity_amount: int | None = None,
        market_pubkey: str | None = None,
        slippage_bps: int = 200,
    ) -> Instruction:
        """KLend liquidate_obligation_and_redeem_reserve_collateral."""
        market = lending_market or Kamino.lending_market()
        market_str = (market_pubkey or str(market)).strip()
        repay_mint_pk = Pubkey.from_string(_mint_str(repay_mint or USDC_MINT_STR))

        if withdraw_liquidity_mint:
            withdraw_mint_pk = Pubkey.from_string(_mint_str(withdraw_liquidity_mint))
        else:
            withdraw_mint_str = await fetch_reserve_liquidity_mint(
                market_str, str(collateral_reserve)
            )
            withdraw_mint_pk = Pubkey.from_string(withdraw_mint_str)

        liq_amt = int(liquidity_amount or 0)
        if liq_amt <= 0:
            partial_pct = float(os.getenv("LIQUIDATION_PARTIAL_PCT", "0.5"))
            partial_pct = max(0.01, min(1.0, partial_pct))
            liq_amt = max(1, int(debt_amt * partial_pct)) if debt_amt else 1
        slip = max(0, min(10_000, int(slippage_bps)))
        min_received = max(1, liq_amt * (10_000 - slip) // 10_000)

        return Kamino.get_liquidate_obligation_ix(
            liquidator=self.user_pubkey,
            obligation=obligation,
            lending_market=market,
            repay_reserve=debt_reserve,
            repay_liquidity_mint=repay_mint_pk,
            withdraw_reserve=collateral_reserve,
            withdraw_liquidity_mint=withdraw_mint_pk,
            liquidity_amount=liq_amt,
            min_acceptable_received_liquidity_amount=min_received,
        )

    async def build_flash_loan_jupiter_route_tx(
        self,
        quote1: dict[str, Any],
        quote2: dict[str, Any],
        quote3: dict[str, Any],
        flash_amount: int,
        executor=None,
    ) -> VersionedTransaction:
        """Flash borrow → three Jupiter swap legs → flash repay (principal + fee)."""
        logger.info(
            "Building Kamino flash loan route TX | Amount: %.2f USDC",
            flash_amount / 1_000_000,
        )

        borrow_ix = await self._get_kamino_flash_borrow_ix(USDC_MINT_STR, flash_amount)
        fee = self._estimate_flash_loan_fee(flash_amount)
        repay_ix = await self._get_kamino_flash_repay_ix(
            USDC_MINT_STR,
            flash_amount + fee,
            borrow_instruction_index=0,
        )

        all_instructions: list[Instruction] = [borrow_ix]
        alt_addresses: list[str] = []

        for i, quote in enumerate([quote1, quote2, quote3], start=1):
            payload = await self._fetch_swap_payload(quote, executor)
            self._ensure_jupiter_swap_payload(payload, leg=i)
            alt_addresses.extend(payload.get("addressLookupTableAddresses") or [])
            if i == 1:
                for cb in payload.get("computeBudgetInstructions") or []:
                    all_instructions.append(self._convert_jupiter_ix(cb))
            for setup in payload.get("setupInstructions") or []:
                all_instructions.append(self._convert_jupiter_ix(setup))
            all_instructions.append(self._convert_jupiter_ix(payload["swapInstruction"]))
            if cleanup := payload.get("cleanupInstruction"):
                all_instructions.append(self._convert_jupiter_ix(cleanup))
            logger.info("Leg %s Jupiter instructions added", i)

        all_instructions.append(repay_ix)
        all_instructions = self._add_compute_budget(all_instructions)

        alts = await self._resolve_alts(alt_addresses)
        recent_blockhash = (await self.client.get_latest_blockhash()).value.blockhash

        message = MessageV0.try_compile(
            payer=self.user_pubkey,
            instructions=all_instructions,
            address_lookup_table_accounts=alts,
            recent_blockhash=recent_blockhash,
        )

        tx = VersionedTransaction(message, [self.keypair])
        logger.info("Full flash-loan route TX built")
        return tx

    @staticmethod
    def _signed_vtx_byte_len(message: MessageV0, signer: Keypair) -> int:
        """Serialized VersionedTransaction size (Solana limit applies to this, not message alone)."""
        return len(bytes(VersionedTransaction(message, [signer])))

    def _collateral_flash_amounts(self, flash_amount: int) -> list[int]:
        """Try full size first, then smaller notionals when the bundle exceeds the tx byte cap."""
        min_micro = max(
            1_000_000,
            int(os.getenv("COLLATERAL_MIN_FLASH_USDC_MICRO", "5000000")),
        )
        raw = (os.getenv("COLLATERAL_FLASH_SIZE_FRACTIONS") or "1,0.75,0.5").strip()
        fractions: list[float] = []
        for part in raw.split(","):
            try:
                frac = float(part.strip())
            except ValueError:
                continue
            if 0 < frac <= 1:
                fractions.append(frac)
        if not fractions:
            fractions = [1.0]
        amounts: list[int] = []
        seen: set[int] = set()
        for frac in fractions:
            amt = max(min_micro, int(flash_amount * frac))
            if amt not in seen:
                seen.add(amt)
                amounts.append(amt)
        return amounts

    async def build_collateral_swap_tx(
        self,
        borrow_reserve_mint: str,
        target_collateral_mint: str,
        flash_amount: int,
        executor=None,
        slippage_bps: int | None = None,
        *,
        borrow_reserve_pubkey: str | None = None,
        lending_market_pubkey: str | None = None,
        swap_amount: int | None = None,
    ) -> VersionedTransaction:
        """Collateral swap flow — Jupiter quote (lite-api) then swap-instructions via executor."""
        logger.info(
            "Building collateral swap | borrow=%s target=%s",
            borrow_reserve_mint[:8],
            target_collateral_mint[:8],
        )

        bps = (
            slippage_bps
            if slippage_bps is not None
            else int(os.getenv("JUPITER_SLIPPAGE_BPS", "50"))
        )
        prefer_direct = os.getenv("COLLATERAL_ONLY_DIRECT_ROUTES", "true").lower() in (
            "1",
            "true",
            "yes",
        )
        try:
            compact_accounts = int(os.getenv("COLLATERAL_JUPITER_MAX_ACCOUNTS", "28"))
        except (TypeError, ValueError):
            compact_accounts = 28
        try:
            relaxed_accounts = int(os.getenv("COLLATERAL_JUPITER_MAX_ACCOUNTS_RELAXED", "48"))
        except (TypeError, ValueError):
            relaxed_accounts = 48
        include_cleanup = os.getenv("COLLATERAL_INCLUDE_JUPITER_CLEANUP", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        max_raw = int(os.getenv("SOLANA_MAX_TX_BYTES", "1232"))
        from src.core.rpc_config import call_with_rpc_fallback

        async def _blockhash(rpc_url: str):
            async with AsyncClient(rpc_url) as rpc_client:
                return (await rpc_client.get_latest_blockhash()).value.blockhash

        recent_blockhash = await call_with_rpc_fallback(
            "sim", _blockhash, label="collateral_blockhash"
        )

        message: MessageV0 | None = None
        chosen_alts = 0
        chosen_vtx_size = 0
        chosen_flash = flash_amount
        last_quote_err: Exception | None = None
        last_size = 0

        from spl.token.instructions import create_idempotent_associated_token_account

        market_pk = (lending_market_pubkey or "").strip() or None
        borrow_mint_pk = Pubkey.from_string(borrow_reserve_mint)
        pre_ixs: list[Instruction] = [
            create_idempotent_associated_token_account(
                self.user_pubkey,
                self.user_pubkey,
                borrow_mint_pk,
            )
        ]
        borrow_ix_index = len(pre_ixs)
        supply_vault: Pubkey | None = None
        fee_vault: Pubkey | None = None
        if borrow_reserve_pubkey:
            supply_vault, fee_vault = await Kamino.fetch_reserve_liquidity_vaults(
                self.keypair,
                borrow_reserve_pubkey,
            )

        for trade_micro in self._collateral_flash_amounts(flash_amount):
            quote_micro = (
                min(int(swap_amount), trade_micro)
                if swap_amount is not None
                else trade_micro
            )
            if quote_micro <= 0:
                continue
            borrow_ix = await self._get_kamino_flash_borrow_ix(
                borrow_reserve_mint,
                trade_micro,
                reserve_pubkey=borrow_reserve_pubkey,
                lending_market_pubkey=market_pk,
                supply_vault=supply_vault,
                fee_vault=fee_vault,
            )
            repay_ix = await self._get_kamino_flash_repay_ix(
                borrow_reserve_mint,
                trade_micro,
                borrow_instruction_index=borrow_ix_index,
                reserve_pubkey=borrow_reserve_pubkey,
                lending_market_pubkey=market_pk,
                supply_vault=supply_vault,
                fee_vault=fee_vault,
            )
            quote_attempts = (
                (True, 16, True),
                (True, max(16, compact_accounts - 8), True),
                (prefer_direct, compact_accounts, True),
                (prefer_direct, compact_accounts, False),
                (prefer_direct, relaxed_accounts, True),
                (False, relaxed_accounts, True),
                (False, None, False),
            )
            for only_direct, max_accounts, use_shared in quote_attempts:
                try:
                    swap_quote = await self._fetch_jupiter_quote(
                        borrow_reserve_mint,
                        target_collateral_mint,
                        quote_micro,
                        slippage_bps=bps,
                        only_direct=only_direct,
                        max_accounts=max_accounts,
                    )
                except ValueError as exc:
                    last_quote_err = exc
                    if "no routes" not in str(exc).lower():
                        raise
                    continue

                payload = await self._fetch_swap_payload(
                    swap_quote,
                    executor,
                    swap_slippage_bps=bps,
                    use_shared_accounts=use_shared,
                )
                self._ensure_jupiter_swap_payload(payload, leg=1)
                swap_ixs: list[Instruction] = []
                for setup in payload.get("setupInstructions") or []:
                    swap_ixs.append(self._convert_jupiter_ix(setup))
                swap_ixs.append(self._convert_jupiter_ix(payload["swapInstruction"]))
                if include_cleanup and (cleanup := payload.get("cleanupInstruction")):
                    swap_ixs.append(self._convert_jupiter_ix(cleanup))

                all_instructions = self._add_compute_budget(
                    [*pre_ixs, borrow_ix, *swap_ixs, repay_ix]
                )
                alt_addresses: list[str] = list(payload.get("addressLookupTableAddresses") or [])
                alts = await self._resolve_alts(alt_addresses)
                candidate = MessageV0.try_compile(
                    payer=self.user_pubkey,
                    instructions=all_instructions,
                    address_lookup_table_accounts=alts,
                    recent_blockhash=recent_blockhash,
                )
                last_size = self._signed_vtx_byte_len(candidate, self.keypair)
                if last_size <= max_raw:
                    message = candidate
                    chosen_alts = len(alts)
                    chosen_vtx_size = last_size
                    chosen_flash = trade_micro
                    break
                logger.debug(
                    "Collateral quote too large | micro=%s direct=%s max_accts=%s shared=%s vtx=%sB alts=%s",
                    trade_micro,
                    only_direct,
                    max_accounts,
                    use_shared,
                    last_size,
                    len(alts),
                )
            if message is not None:
                break

        if message is None:
            if last_quote_err and last_size == 0:
                raise last_quote_err
            raise ValueError(
                f"Collateral TX too large ({last_size}B > {max_raw}B) after Jupiter quote attempts. "
                "Lower COLLATERAL_FLASH_AMOUNT_USDC_MICRO or COLLATERAL_JUPITER_MAX_ACCOUNTS."
            )

        tx = VersionedTransaction(message, [self.keypair])
        logger.info(
            "Collateral swap TX built | vtx=%sB msg=%sB alts=%s flash_micro=%s",
            chosen_vtx_size,
            len(bytes(message)),
            chosen_alts,
            chosen_flash,
        )
        return tx

    async def _get_kamino_flash_borrow_ix(
        self,
        mint: str,
        amount: int,
        *,
        reserve_pubkey: str | None = None,
        lending_market_pubkey: str | None = None,
        supply_vault: Pubkey | str | None = None,
        fee_vault: Pubkey | str | None = None,
    ) -> Instruction:
        return Kamino.get_flash_borrow_ix(
            mint,
            amount,
            self.user_pubkey,
            reserve_pubkey=reserve_pubkey,
            lending_market_pubkey=lending_market_pubkey,
            supply_vault=supply_vault,
            fee_vault=fee_vault,
        )

    async def _get_kamino_flash_repay_ix(
        self,
        mint: str,
        amount: int,
        *,
        borrow_instruction_index: int = 0,
        reserve_pubkey: str | None = None,
        lending_market_pubkey: str | None = None,
        supply_vault: Pubkey | str | None = None,
        fee_vault: Pubkey | str | None = None,
    ) -> Instruction:
        return Kamino.get_flash_repay_ix(
            mint,
            amount,
            self.user_pubkey,
            borrow_ix_index=borrow_instruction_index,
            reserve_pubkey=reserve_pubkey,
            lending_market_pubkey=lending_market_pubkey,
            supply_vault=supply_vault,
            fee_vault=fee_vault,
        )

    async def _fetch_swap_payload(
        self,
        quote: dict[str, Any],
        executor=None,
        swap_slippage_bps: int | None = None,
        *,
        use_shared_accounts: bool = False,
    ) -> dict[str, Any]:
        if executor is not None and hasattr(executor, "get_swap_instructions"):
            getter = executor.get_swap_instructions
            if swap_slippage_bps is not None:
                data = await getter(quote, swap_slippage_bps)
            else:
                data = await getter(quote)
            return data if isinstance(data, dict) else {}
        url = "https://api.jup.ag/swap/v1/swap-instructions"
        body: dict[str, Any] = {
            "quoteResponse": quote,
            "userPublicKey": str(self.user_pubkey),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "skipUserAccountsRpcChecks": True,
        }
        if use_shared_accounts:
            body["useSharedAccounts"] = True
        key = (os.getenv("JUPITER_API_KEY") or "").strip()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if key:
            headers["x-api-key"] = key
        retries = max(1, int(os.getenv("JUPITER_SWAP_IX_MAX_RETRIES", "5")))
        delay = float(os.getenv("JUPITER_SWAP_IX_RETRY_DELAY_SEC", "0.6"))
        async with aiohttp.ClientSession() as session:
            for attempt in range(retries):
                async with session.post(
                    url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=35)
                ) as resp:
                    raw = await resp.text()
                    if resp.status == 429 or resp.status in (500, 502, 503, 504):
                        await asyncio.sleep(delay * (2**attempt))
                        continue
                    if resp.status != 200:
                        return {"error": f"HTTP {resp.status}: {raw[:400]}"}
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return {"error": raw[:400]}
            return {"error": "swap-instructions retries exhausted"}

    async def _get_jupiter_swap_instructions(
        self, quote: dict[str, Any], executor=None
    ) -> list[Instruction]:
        payload = await self._fetch_swap_payload(quote, executor)
        self._ensure_jupiter_swap_payload(payload, leg=1)
        instructions: list[Instruction] = []
        for ix_data in payload.get("computeBudgetInstructions") or []:
            instructions.append(self._convert_jupiter_ix(ix_data))
        for ix_data in payload.get("setupInstructions") or []:
            instructions.append(self._convert_jupiter_ix(ix_data))
        instructions.append(self._convert_jupiter_ix(payload["swapInstruction"]))
        if cleanup := payload.get("cleanupInstruction"):
            instructions.append(self._convert_jupiter_ix(cleanup))
        return instructions

    def _ensure_jupiter_swap_payload(self, payload: dict[str, Any], *, leg: int) -> None:
        if not isinstance(payload, dict):
            raise ValueError(f"Leg {leg}: Jupiter swap-instructions response is not an object")
        err = payload.get("error")
        if err is not None:
            raise ValueError(f"Leg {leg}: Jupiter API error: {err}")
        if not payload.get("swapInstruction"):
            raise ValueError(
                f"Leg {leg}: missing swapInstruction (stale quote or route unavailable)"
            )

    def _convert_jupiter_ix(self, jup_ix: dict[str, Any]) -> Instruction:
        program_id = Pubkey.from_string(jup_ix["programId"])
        accounts = [
            AccountMeta(
                pubkey=Pubkey.from_string(a["pubkey"]),
                is_signer=bool(a.get("isSigner", False)),
                is_writable=bool(a.get("isWritable", False)),
            )
            for a in jup_ix.get("accounts", [])
        ]
        data_b64 = jup_ix.get("data") or ""
        data = base64.b64decode(data_b64) if data_b64 else b""
        return Instruction(program_id=program_id, accounts=accounts, data=data)

    def _add_compute_budget(self, instructions: list[Instruction]) -> list[Instruction]:
        return instructions

    def _estimate_flash_loan_fee(self, amount: int) -> int:
        flash_fee_bps = int(os.getenv("KAMINO_FLASH_LOAN_FEE_BPS", "5"))
        return max(1, (amount * flash_fee_bps) // 10_000)

    async def _resolve_alts_once(
        self, alt_addresses: Iterable[str]
    ) -> list[AddressLookupTableAccount]:
        """Single get_multiple_accounts + deserialize pass for Jupiter ALT addresses."""
        unique: list[str] = []
        seen: set[str] = set()
        for alt in alt_addresses:
            if alt and alt not in seen:
                seen.add(alt)
                unique.append(alt)
        if not unique:
            return []

        pubkeys = [Pubkey.from_string(a) for a in unique]
        from src.core.rpc_config import call_with_rpc_fallback

        async def _fetch_alts(rpc_url: str):
            async with AsyncClient(rpc_url) as rpc_client:
                return await rpc_client.get_multiple_accounts(pubkeys)

        resp = await call_with_rpc_fallback("default", _fetch_alts, label="kamino_alt_resolve")
        rows = resp.value or []

        resolved: list[AddressLookupTableAccount] = []
        for table_key, account_info in zip(pubkeys, rows):
            if account_info is None:
                continue
            try:
                raw = account_info.data
                table_state = AddressLookupTable.deserialize(
                    raw if isinstance(raw, (bytes, bytearray)) else bytes(raw)
                )
                resolved.append(
                    AddressLookupTableAccount(key=table_key, addresses=table_state.addresses)
                )
            except Exception:
                continue
        return resolved

    async def _resolve_alts(self, alt_addresses: Iterable[str]) -> list[AddressLookupTableAccount]:
        """Deserialize LUT accounts for MessageV0.try_compile (solders types); retries RPC flakes."""
        alt_address_strings = list(alt_addresses)
        if not alt_address_strings:
            return []

        max_attempts = max(1, int(os.getenv("KAMINO_ALT_RESOLVE_MAX_ATTEMPTS", "3")))
        delay_sec = float(os.getenv("KAMINO_ALT_RESOLVE_RETRY_DELAY_SEC", "0.5"))
        for attempt in range(max_attempts):
            try:
                return await self._resolve_alts_once(alt_address_strings)
            except Exception as e:
                logger.warning("ALT resolve attempt %s failed: %s", attempt, e)
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(delay_sec)
        return []

    async def _fetch_jupiter_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        *,
        slippage_bps: int | None = None,
        only_direct: bool = False,
        max_accounts: int | None = None,
    ) -> dict[str, Any]:
        bps = (
            slippage_bps
            if slippage_bps is not None
            else int(os.getenv("JUPITER_SLIPPAGE_BPS", "50"))
        )
        quote_url = os.getenv("JUPITER_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote")
        params: dict[str, str] = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(bps),
        }
        if only_direct:
            params["onlyDirectRoutes"] = "true"
        if max_accounts and max_accounts > 0:
            params["maxAccounts"] = str(max_accounts)
        if os.getenv("COLLATERAL_RESTRICT_INTERMEDIATE_TOKENS", "true").lower() in (
            "1",
            "true",
            "yes",
        ):
            params["restrictIntermediateTokens"] = "true"
        key = (os.getenv("JUPITER_API_KEY") or "").strip()
        headers = {"Accept": "application/json"}
        if key:
            headers["x-api-key"] = key
        retries = max(1, int(os.getenv("JUPITER_QUOTE_MAX_RETRIES", "5")))
        delay = float(os.getenv("JUPITER_QUOTE_RETRY_DELAY_SEC", "0.6"))
        data: dict[str, Any] = {}
        async with aiohttp.ClientSession() as session:
            for attempt in range(retries):
                async with session.get(
                    quote_url,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=25),
                ) as resp:
                    raw = await resp.text()
                    if resp.status == 429 or resp.status in (500, 502, 503, 504):
                        await asyncio.sleep(delay * (2**attempt))
                        continue
                    if resp.status != 200:
                        raise ValueError(f"Jupiter quote HTTP {resp.status}: {raw[:400]}")
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"Jupiter quote invalid JSON: {raw[:200]}") from exc
                    break
            else:
                raise ValueError("Jupiter quote retries exhausted (429/rate limit or RPC errors)")
        if not isinstance(data, dict):
            raise ValueError("Jupiter quote response is not an object")
        if data.get("error"):
            raise ValueError(f"Jupiter quote error: {data.get('error')}")
        return data


async def test_kamino():
    from src.config.settings import bootstrap_config, settings

    bootstrap_config()
    client = AsyncClient(settings.SOLANA_RPC_URL)
    keypair = Keypair.from_base58_string(settings.active_private_key)

    _ = KaminoFlashLoan(client, keypair)
    logger.info("Kamino helper ready (flash loan + collateral swap)")
