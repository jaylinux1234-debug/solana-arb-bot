# executor.py
import base64
import logging

from jupiter_python_sdk.jupiter import Jupiter
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from src.config.settings import (
    MAX_SLIPPAGE_BPS,
    PRIORITY_FEE_LAMPORTS,
)
from src.core.circuit_breaker import circuit_breaker
from src.execution.jito import send_jito_bundle


async def execute_with_flash_loan_and_jito(
    jup: Jupiter,
    client: AsyncClient,
    keypair: Keypair,
    flash_loan_amount: int,  # e.g. 1_000_000_000 (1 SOL or USDC equivalent)
    flash_loan_mint: str,  # Token you want to borrow (usually USDC or SOL)
    route_steps: list,  # List of swap dicts from Jupiter
    jito_tip_lamports: int = 50_000,  # Tip to Jito (adjust based on congestion)
):
    """
    Full atomic execution: Flash Loan + Triangular Swaps + Jito Bundle
    """
    print("🚀 Preparing Flash Loan + Jupiter + Jito Bundle...")

    try:
        # 1. Get Flash Loan Instructions from Kamino
        # Note: Kamino mainly provides TS SDK. We build raw instructions here.
        # For simplicity, we use direct program calls (advanced).
        # Alternative: Use Jupiter Lend flash loans if available.

        # === STEP 1: Build Jupiter Swap Instructions for all legs ===
        swap_instructions = []
        for step in route_steps:
            swap_tx = await jup.swap(
                input_mint=step["input_mint"],
                output_mint=step["output_mint"],
                amount=step["amount"],
                slippage_bps=step.get("slippage_bps", 100),
            )
            # Extract instructions from the swap response
            # (This part needs adaptation based on jupiter-python-sdk output)
            swap_instructions.extend(swap_tx.instructions)  # Pseudo-code

        # 2. Build Flash Loan Borrow + Repay (Kamino)
        # This is complex — here's simplified structure:
        flash_borrow_ix = await get_kamino_flash_borrow_ix(flash_loan_mint, flash_loan_amount)
        flash_repay_ix = await get_kamino_flash_repay_ix(flash_loan_mint, flash_loan_amount)

        # 3. Combine everything into one Versioned Transaction
        all_instructions = [flash_borrow_ix] + swap_instructions + [flash_repay_ix]

        # Add Compute Budget + Priority Fee + Jito Tip
        tx = await build_versioned_tx_with_priority_fees(
            client, keypair, all_instructions, jito_tip_lamports
        )

        # 4. Send as Jito Bundle (Block Engine expects bundle with tip tx appended when using defaults).
        bundle_id = await send_jito_bundle(
            [tx],
            client=client,
            keypair=keypair,
            tip_lamports=jito_tip_lamports,
        )

        print(f"✅ Jito bundle submitted — bundle_id={bundle_id}")
        return bundle_id

    except Exception as e:
        print(f"❌ Execution failed: {e}")
        return None


logger = logging.getLogger(__name__)


class TradeExecutor:
    def __init__(self, jupiter: Jupiter, client: AsyncClient, keypair: Keypair):
        self.jupiter = jupiter
        self.client = client
        self.keypair = keypair

    async def execute_jupiter_swap(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,  # in smallest units (e.g. 1_000_000_000 = 1 USDC)
        slippage_bps: int = None,
    ) -> str | None:
        """Simple direct swap (no flash loan yet)"""
        slippage_bps = slippage_bps or MAX_SLIPPAGE_BPS

        if circuit_breaker.should_pause():
            logger.warning("Circuit breaker active — skipping TradeExecutor Jupiter swap")
            return None

        try:
            # 1. Get quote + swap transaction from Jupiter
            swap_result = await self.jupiter.swap(
                input_mint=input_mint,
                output_mint=output_mint,
                amount=amount,
                slippage_bps=slippage_bps,
                prioritization_fee_lamports=PRIORITY_FEE_LAMPORTS,
            )

            if not swap_result:
                logger.error("Failed to get swap transaction from Jupiter")
                return None

            # The SDK usually returns serialized tx as string/base64
            if isinstance(swap_result, str):
                tx_bytes = base64.b64decode(swap_result)
            else:
                # Adjust based on your SDK version
                tx_bytes = base64.b64decode(swap_result.get("swapTransaction"))

            # 2. Deserialize and sign
            tx = VersionedTransaction.from_bytes(tx_bytes)
            tx.sign([self.keypair])

            # 3. Send transaction
            tx_sig = await self.client.send_raw_transaction(
                tx.serialize(), opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
            )

            logger.info(f"✅ Swap sent! Signature: {tx_sig.value}")
            return str(tx_sig.value)

        except Exception as e:
            logger.error(f"Swap execution failed: {e}")
            return None


# Helper to create executor instance
async def create_executor():
    from solders.keypair import Keypair

    from src.config.settings import settings
    from src.core.security import validate_bot_environment

    rpc = settings.SOLANA_RPC_URL
    pk = settings.active_private_key
    test_mode = settings.test_mode
    if not rpc or not pk:
        raise ValueError("Missing SOLANA_RPC_URL or PRIVATE_KEY")
    validate_bot_environment(rpc_url=rpc, private_key=pk, test_mode=test_mode)
    keypair = Keypair.from_base58_string(pk)
    client = AsyncClient(rpc)

    jup = Jupiter(async_client=client, keypair=keypair)

    return TradeExecutor(jupiter=jup, client=client, keypair=keypair)
