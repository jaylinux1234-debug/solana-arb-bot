from src.core.sizing import get_max_trade_size_usdc

pairs = ["BONK", "WIF", "POPCAT", "MEW", "FARTCOIN", "SOL"]
for p in pairs:
    print(f"{p:10} -> ${get_max_trade_size_usdc(p):.2f}")
