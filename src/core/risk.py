# src/core/risk.py
"""Production risk engine — drawdown, circuit breaker, cooldown, inventory."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DynamicSizer:
    def __init__(self, min_usdc: int = 30000, max_usdc: int = 500000):
        self.min_usdc = min_usdc
        self.max_usdc = max_usdc

    async def compute_optimal_size(
        self, net_bps: float, inventory_sol: float, volatility_bps: float
    ) -> int:
        """Smart position sizing based on edge strength, inventory, volatility."""
        edge_factor = min(1.0, net_bps / 110)
        base_size = int(self.max_usdc * edge_factor)
        vol_factor = max(0.4, 1.0 - (volatility_bps / 220))
        size = int(base_size * vol_factor)

        if inventory_sol > 32:
            size = int(size * 0.65)
        elif inventory_sol > 25:
            size = int(size * 0.85)

        size = max(self.min_usdc, min(self.max_usdc, size))
        logger.info(
            "Dynamic size: %.1fk USDC | edge=%.1fbps | vol=%.0f",
            size / 1_000_000,
            net_bps,
            volatility_bps,
        )
        return size


class RiskEngine:
    """Production risk engine — drawdown protection, circuit breaker, inventory control."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.state_path = Path("logs/risk_state.json")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.daily_pnl: float = 0.0
        self.loss_streak: int = 0
        self.last_reset: datetime = _utcnow()
        self.total_trades_today: int = 0
        self.last_trade_time: datetime | None = None
        self.load_state()

    def load_state(self) -> None:
        """Load risk state from disk."""
        try:
            if not self.state_path.exists():
                return
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.daily_pnl = float(data.get("daily_pnl", 0.0))
            self.loss_streak = int(data.get("loss_streak", 0))
            self.total_trades_today = int(data.get("total_trades_today", 0))
            last_reset_str = data.get("last_reset") or data.get("timestamp")
            if last_reset_str:
                self.last_reset = datetime.fromisoformat(
                    str(last_reset_str).replace("Z", "+00:00")
                )
            last_trade_str = data.get("last_trade_time")
            if last_trade_str:
                self.last_trade_time = datetime.fromisoformat(
                    str(last_trade_str).replace("Z", "+00:00")
                )
        except Exception as exc:
            logger.warning("Failed to load risk state: %s", exc)

    def save_state(self) -> None:
        """Persist risk state."""
        try:
            data = {
                "daily_pnl": self.daily_pnl,
                "loss_streak": self.loss_streak,
                "total_trades_today": self.total_trades_today,
                "last_reset": self.last_reset.isoformat(),
                "last_trade_time": (
                    self.last_trade_time.isoformat() if self.last_trade_time else None
                ),
                "timestamp": _utcnow().isoformat(),
            }
            self.state_path.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("Failed to save risk state: %s", exc)

    def reset_daily(self) -> None:
        """Reset daily counters after 24h."""
        now = _utcnow()
        if now - self.last_reset > timedelta(days=1):
            self.daily_pnl = 0.0
            self.loss_streak = 0
            self.total_trades_today = 0
            self.last_reset = now
            self.save_state()
            logger.info("RiskEngine: daily counters reset")

    @staticmethod
    def _to_usdc_micro(proposed_size_usdc: float) -> float:
        """Normalize size: accept USDC dollars or micro-units."""
        if proposed_size_usdc <= 0:
            return 0.0
        if proposed_size_usdc >= 1_000_000:
            return float(proposed_size_usdc)
        return float(proposed_size_usdc) * 1_000_000.0

    def can_trade(self, proposed_size_usdc: float = 0.0) -> bool:
        """Main risk gate — return True if trade is allowed."""
        self.reset_daily()

        max_loss = float(self.settings.risk.max_daily_loss_usdc)
        if self.daily_pnl <= -max_loss:
            logger.warning("DAILY LOSS LIMIT HIT: $%.2f (cap $%.2f)", self.daily_pnl, max_loss)
            return False

        streak_cap = int(self.settings.risk.circuit_breaker_loss_streak)
        if self.loss_streak >= streak_cap:
            logger.warning("CIRCUIT BREAKER ACTIVE: %d losses", self.loss_streak)
            return False

        max_per_hour = int(self.settings.trading.max_live_trades_per_hour)
        if max_per_hour > 0:
            rough_daily_cap = max_per_hour * 24
            if self.total_trades_today >= rough_daily_cap:
                logger.warning("Max daily trades reached (%d)", rough_daily_cap)
                return False

        if self.last_trade_time:
            cooldown = (_utcnow() - self.last_trade_time).total_seconds()
            if cooldown < float(self.settings.trading.live_trade_cooldown_seconds):
                logger.debug("Trade blocked: cooldown %.1fs remaining", cooldown)
                return False

        proposed_micro = self._to_usdc_micro(proposed_size_usdc)
        max_micro = float(self.settings.trading.max_flash_usdc) * 1_000_000.0
        if proposed_micro > max_micro > 0:
            logger.warning(
                "Trade size %.0f micro exceeds max flash %.0f micro",
                proposed_micro,
                max_micro,
            )
            return False

        return True

    def record_trade_result(
        self,
        pnl_usdc: float,
        size_usdc: float = 0.0,
    ) -> None:
        """Record outcome of a trade."""
        _ = size_usdc
        self.reset_daily()
        self.daily_pnl += float(pnl_usdc)
        self.total_trades_today += 1
        self.last_trade_time = _utcnow()
        if pnl_usdc < 0:
            self.loss_streak += 1
            logger.warning("Loss recorded: $%.2f | streak=%d", pnl_usdc, self.loss_streak)
        else:
            self.loss_streak = 0
            logger.info("Win recorded: +$%.2f", pnl_usdc)
        self.save_state()

    def within_inventory_limit(self, inventory_sol: float) -> bool:
        cap = float(self.settings.trading.max_inventory_sol)
        if inventory_sol > cap:
            logger.info("Inventory cap exceeded: %.2f SOL > %.2f", inventory_sol, cap)
            return False
        return True

    def get_status(self) -> dict[str, Any]:
        """Health check / metrics payload."""
        return {
            "daily_pnl": round(self.daily_pnl, 2),
            "loss_streak": self.loss_streak,
            "total_trades_today": self.total_trades_today,
            "can_trade": self.can_trade(0),
            "last_reset": self.last_reset.isoformat(),
            "last_trade_time": (
                self.last_trade_time.isoformat() if self.last_trade_time else None
            ),
        }
