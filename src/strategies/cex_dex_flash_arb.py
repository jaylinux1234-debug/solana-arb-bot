# Inside your CexDexFlashArb class or main cycle

from src.core.flash_loan_sizer import FlashLoanSizer


async def calculate_flash_loan_size(self, opportunity: dict) -> int:
    """New dynamic sizing logic"""
    gross_bps = opportunity.get("gross_bps", 0)
    
    size_micro = await FlashLoanSizer.get_optimal_size(
        opportunity_type="cex_dex",
        expected_gross_bps=gross_bps
    )
    
    # Final safety clamps
    size_micro = min(
        size_micro,
        settings.MAX_SINGLE_TRADE_USDC_MICRO,
        settings.CEX_DEX_MAX_TRADE_USDC_MICRO
    )
    
    return int(size_micro)


async def execute_flash_loan_trade(self, opportunity: dict):
    flash_amount = await self.calculate_flash_loan_size(opportunity)
    
    logger.info(f"Executing flash loan of {(flash_amount/1_000_000):.0f} USDC")
    
    # Build transaction with dynamic amount
    tx = await self.build_flash_loan_tx(
        flash_amount_usdc_micro=flash_amount,
        opportunity=opportunity
    )
    
    # Simulate first
    sim = await self.client.simulate_transaction(tx)
    if sim.value.err:
        logger.warning("Simulation failed - reducing size")
        flash_amount = flash_amount * 70 // 100  # 30% reduction
        tx = await self.build_flash_loan_tx(flash_amount, opportunity)
    
    # Send via Jito bundle
    result = await self.send_jito_bundle(tx)
    return result