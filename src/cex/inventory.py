"""CEX-attributed SOL inventory (logical fills). Keeps sizing from exceeding configured caps."""

from __future__ import annotations


class CexInventoryTracker:
    """Tracks estimated free SOL on the CEX attributable to this bot's orders."""

    __slots__ = ("_sol",)

    def __init__(self) -> None:
        self._sol = 0.0

    @property
    def sol_estimate(self) -> float:
        return self._sol

    def seed(self, sol_free: float) -> None:
        """Replace estimate with exchange-reported free SOL (startup sync)."""
        self._sol = max(0.0, float(sol_free))

    def record_buy_sol(self, amount: float) -> None:
        self._sol += max(0.0, float(amount))

    def record_sell_sol(self, amount: float) -> None:
        self._sol = max(0.0, self._sol - max(0.0, float(amount)))

    def cap_trade_usdc_micro(
        self, usdc_micro: int, cex_mid_usdc_per_sol: float, max_inventory_sol: float
    ) -> int:
        """Shrink USDC notional so post-buy SOL does not exceed ``max_inventory_sol``."""
        if max_inventory_sol <= 0 or cex_mid_usdc_per_sol <= 0:
            return usdc_micro
        sol_if_trade = usdc_micro / (cex_mid_usdc_per_sol * 1_000_000)
        room = max(0.0, max_inventory_sol - self._sol)
        if sol_if_trade <= room:
            return usdc_micro
        cap_usdc = int(room * cex_mid_usdc_per_sol * 1_000_000)
        return max(0, min(usdc_micro, cap_usdc))
