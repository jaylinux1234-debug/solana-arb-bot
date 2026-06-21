"""Shared dynamic position manager with tiered TP + trailing stop."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.strategies.meme_sniping.config import meme_sniping_settings
from src.strategies.meme_sniping.execution import (
    active_positions,
    execute_snipe,
    partial_sell,
    sell_position,
)
from src.strategies.meme_sniping.sources import fetch_token_mark_price_usd

logger = logging.getLogger(__name__)


@dataclass
class ManagedPosition:
    token_mint: str
    entry_price: float
    size_sol: float
    lane: str
    entry_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    tp_levels_bps: list[int] = field(default_factory=list)
    tp_taken: list[bool] = field(default_factory=list)
    trailing_active: bool = False
    trailing_peak_bps: float = 0.0
    simulated: bool = True

    def __post_init__(self) -> None:
        if not self.tp_levels_bps:
            self.tp_levels_bps = meme_sniping_settings.tp_levels_bps
        if not self.tp_taken:
            self.tp_taken = [False] * len(self.tp_levels_bps)


class PositionManager:
    """Cross-lane position tracker (meme sniping, copy, migration, filter)."""

    def __init__(self) -> None:
        self.positions: dict[str, ManagedPosition] = {}

    async def track_new_position(
        self,
        token_mint: str,
        entry_price: float,
        size_sol: float,
        *,
        lane: str = "meme_lane",
        simulated: bool | None = None,
    ) -> None:
        sim = meme_sniping_settings.simulate if simulated is None else simulated
        self.positions[token_mint] = ManagedPosition(
            token_mint=token_mint,
            entry_price=entry_price,
            size_sol=size_sol,
            lane=lane,
            simulated=sim,
        )
        logger.info(
            "position_manager track | lane=%s mint=%s size_sol=%.3f entry=%.8f sim=%s",
            lane,
            token_mint[:12],
            size_sol,
            entry_price,
            sim,
        )

    async def open_via_execution(self, token_mint: str, size_sol: float, *, lane: str) -> None:
        """Delegate entry to meme_sniping execution engine."""
        price = await fetch_token_mark_price_usd(token_mint)
        await execute_snipe(token_mint, size_sol)
        await self.track_new_position(
            token_mint,
            float(price or 1.0),
            size_sol,
            lane=lane,
            simulated=meme_sniping_settings.simulate,
        )

    async def monitor_positions(self) -> None:
        """Poll all managed positions (used when not on execution monitor)."""
        cfg = meme_sniping_settings
        for mint, pos in list(self.positions.items()):
            exec_pos = active_positions.get(mint)
            if exec_pos:
                continue

            price = await fetch_token_mark_price_usd(mint)
            if not price or pos.entry_price <= 0:
                continue
            profit_bps = (price - pos.entry_price) / pos.entry_price * 10_000.0

            fractions = cfg.tp_partial_fractions
            for i, tp in enumerate(pos.tp_levels_bps):
                if pos.tp_taken[i] or profit_bps < tp:
                    continue
                frac = fractions[i] if i < len(fractions) else 1.0
                reason = f"TP{i + 1} (+{tp}bps)"
                if frac >= 0.99 or i == len(pos.tp_levels_bps) - 1:
                    await sell_position(mint, reason, profit_bps)
                    self.positions.pop(mint, None)
                else:
                    await partial_sell(mint, frac, reason, profit_bps)
                pos.tp_taken[i] = True
                break

            if mint not in self.positions:
                continue

            if profit_bps > cfg.trailing_arm_bps and not pos.trailing_active:
                pos.trailing_active = True
                pos.trailing_peak_bps = profit_bps

            if pos.trailing_active:
                if profit_bps > pos.trailing_peak_bps:
                    pos.trailing_peak_bps = profit_bps
                elif profit_bps < pos.trailing_peak_bps - cfg.trailing_stop_bps:
                    await sell_position(mint, "trailing_stop", profit_bps)
                    self.positions.pop(mint, None)

    def stats(self) -> dict[str, Any]:
        return {
            "active_positions": len(self.positions),
            "lanes": {p.lane for p in self.positions.values()},
        }


position_manager = PositionManager()
