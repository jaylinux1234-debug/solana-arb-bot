from src.core.sizing import get_max_trade_size_usdc, calculate_trade_size

print("BONK ->", get_max_trade_size_usdc("BONK"))
print("MICH ->", get_max_trade_size_usdc("MICH"))
print("BONK 8bps ->", calculate_trade_size("BONK", 8) / 1000000)
print("BONK 25bps ->", calculate_trade_size("BONK", 25) / 1000000)
