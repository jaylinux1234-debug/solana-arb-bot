async def run_cycle(self):
    strategies = [CexDexStrategy(), LiquidationStrategy(), ...]
    for strat in sorted_by_priority(strategies):
        signal = await strat.detect()
        if signal and await self.ai_approve(signal):
            await strat.execute()
            break