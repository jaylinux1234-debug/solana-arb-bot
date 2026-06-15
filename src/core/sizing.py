# src/core/sizing.py
"""Dynamic flash-loan / CEX-DEX trade sizing."""


def dynamic_flash_size(
    base: int = 150_000,
    utilization: float = 0.68,
    volatility: float = 80,
) -> int:
    """Sweet spot 30k-500k with volatility adjustment."""
    volatility_factor = max(0.6, 1.0 - (volatility / 300))
    size = int(base * utilization * volatility_factor)
    return max(30_000, min(500_000, size))
