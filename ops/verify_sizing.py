from src.core.sizing import calculate_trade_size, get_max_trade_size_usdc

pairs = ["BONK", "WIF", "POPCAT", "MEW", "SOL"]
for p in pairs:
    print(p, "max ->", get_max_trade_size_usdc(p))
for bps in (8, 15, 20):
    print(f"BONK gross={bps}bps -> ${calculate_trade_size('BONK', bps, 42.0)/1e6:.2f}")
