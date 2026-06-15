"""KLend liquidate_obligation_and_redeem_reserve_collateral ix builder tests."""

from __future__ import annotations

from solders.pubkey import Pubkey

from src.dex.kamino import (
    KAMINO_PROGRAM_ID,
    LIQUIDATE_OBLIGATION_DISCRIMINATOR,
    Kamino,
)


def test_liquidate_ix_discriminator_and_program():
    liquidator = Pubkey.from_string("nu11111111111111111111111111111111111111111")
    obligation = Pubkey.from_string("11111111111111111111111111111112")
    market = Pubkey.from_string("7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF")
    repay_reserve = Pubkey.from_string("D6q6wuQSrifJKZYpR1M8R4YawnLDtDsMmWM1NbBmgJ59")
    repay_mint = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    withdraw_reserve = Pubkey.from_string("d4A2prbA2whesmvHaL88BH6Ewn5N4bTSU2Ze8P6Bc4Q")
    withdraw_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")

    ix = Kamino.get_liquidate_obligation_ix(
        liquidator=liquidator,
        obligation=obligation,
        lending_market=market,
        repay_reserve=repay_reserve,
        repay_liquidity_mint=repay_mint,
        withdraw_reserve=withdraw_reserve,
        withdraw_liquidity_mint=withdraw_mint,
        liquidity_amount=5_000_000,
        min_acceptable_received_liquidity_amount=4_900_000,
    )

    assert ix.program_id == KAMINO_PROGRAM_ID
    assert ix.data[:8] == LIQUIDATE_OBLIGATION_DISCRIMINATOR
    assert int.from_bytes(ix.data[8:16], "little") == 5_000_000
    assert int.from_bytes(ix.data[16:24], "little") == 4_900_000
    assert int.from_bytes(ix.data[24:32], "little") == 0
    assert len(ix.accounts) == 20
    assert ix.accounts[0].pubkey == liquidator
    assert ix.accounts[0].is_signer is True
