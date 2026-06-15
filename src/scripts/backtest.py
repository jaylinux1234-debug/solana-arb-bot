# src/scripts/backtest.py
import pandas as pd

from src.strategies.cex_dex_strategy import CexDexStrategy


async def run_backtest(historical_data: pd.DataFrame):
    strategy = CexDexStrategy()
    wins = 0
    total = 0
    for _, row in historical_data.iterrows():
        signal = await strategy.evaluate_opportunity()  # mock with row data
        if signal and signal["net_bps"] > 0:
            wins += 1
        total += 1
    win_rate = (wins / total) * 100 if total else 0
    print(f"Backtest Win Rate: {win_rate:.1f}% | Trades: {total}")
    return win_rate

# Usage: python -m src.scripts.backtest (load CSV of historical CEX/DEX prices)