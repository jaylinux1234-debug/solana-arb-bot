"""Circuit breaker: pybreaker for monitor ticks + PnL / drawdown pause for live sends."""

from __future__ import annotations

import functools
import logging
import os
import time
from collections.abc import Callable
from typing import Any, TypeVar

import pybreaker

logger = logging.getLogger(__name__)

FAIL_MAX = int(os.getenv("CIRCUIT_BREAKER_FAIL_MAX", "5"))
RESET_TIMEOUT = int(os.getenv("CIRCUIT_BREAKER_RESET_TIMEOUT", "60"))

monitor_breaker = pybreaker.CircuitBreaker(
    fail_max=FAIL_MAX,
    reset_timeout=RESET_TIMEOUT,
    name="monitor",
)

WS_FAIL_MAX = int(os.getenv("WS_BREAKER_FAIL_MAX", str(FAIL_MAX)))
WS_RESET_TIMEOUT = int(os.getenv("WS_BREAKER_RESET_TIMEOUT", str(RESET_TIMEOUT)))

ws_breaker = pybreaker.CircuitBreaker(
    fail_max=WS_FAIL_MAX,
    reset_timeout=WS_RESET_TIMEOUT,
    name="ws",
)

F = TypeVar("F", bound=Callable)


def async_breaker(breaker: pybreaker.CircuitBreaker) -> Callable[[F], F]:
    """Async-safe @breaker for monitor loop coroutines."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            with breaker:
                return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


class TradeCircuitBreaker:
    """
    Live-trade pause gate:

    - Manual trip (drawdown, inventory drift, ops)
    - PnL loss streak
    - Open ``monitor_breaker`` (monitor tick failures)
    """

    def __init__(self) -> None:
        self.loss_streak = 0
        self.last_loss_ts = 0.0
        self._manual_tripped = False
        self._trip_reason: str | None = None
        self._tripped_at: float = 0.0
        self._loss_streak_threshold = max(1, int(os.getenv("CIRCUIT_BREAKER_LOSS_STREAK", "3")))
        self._loss_streak_cooldown_sec = max(
            60.0,
            float(os.getenv("CIRCUIT_BREAKER_LOSS_COOLDOWN_SEC", "300")),
        )
        self._loss_usd_threshold = float(os.getenv("CIRCUIT_BREAKER_LOSS_USD", "10"))

    @property
    def is_tripped(self) -> bool:
        return self._manual_tripped

    @property
    def trip_reason(self) -> str | None:
        return self._trip_reason

    def status(self) -> dict[str, Any]:
        return {
            "manual_tripped": self._manual_tripped,
            "trip_reason": self._trip_reason,
            "tripped_at": self._tripped_at,
            "loss_streak": self.loss_streak,
            "monitor_breaker_state": monitor_breaker.current_state,
            "should_pause": self.should_pause(),
        }

    def trip(self, reason: str = "manual") -> None:
        """Hard-pause live sends until ``reset(force=True)``."""
        self._manual_tripped = True
        self._trip_reason = str(reason)[:500]
        self._tripped_at = time.time()
        logger.critical("Circuit breaker TRIPPED: %s", self._trip_reason)
        try:
            from src.monitoring.metrics import set_circuit_breaker_tripped
            from src.monitoring.telegram_alerts import schedule_telegram

            set_circuit_breaker_tripped(True)
            if os.getenv("TELEGRAM_CIRCUIT_BREAKER_ALERT", "true").lower() in (
                "1",
                "true",
                "yes",
            ):
                schedule_telegram(
                    f"Circuit breaker TRIPPED\nReason: {self._trip_reason}",
                    dedupe_key="circuit_breaker",
                    cooldown_sec=600.0,
                )
        except Exception:
            pass

    def reset(self, *, force: bool = False) -> bool:
        """
        Clear manual trip. Requires ``force=True`` after drawdown/inventory trips
        unless ``CIRCUIT_BREAKER_AUTO_RESET=true``.
        """
        auto = os.getenv("CIRCUIT_BREAKER_AUTO_RESET", "").lower() in ("1", "true", "yes")
        if self._manual_tripped and not force and not auto:
            logger.warning(
                "Circuit breaker reset refused (manual trip active: %s). Use force=True.",
                self._trip_reason,
            )
            return False
        if self._manual_tripped:
            logger.warning("Circuit breaker reset | was: %s", self._trip_reason)
        self._manual_tripped = False
        self._trip_reason = None
        self._tripped_at = 0.0
        self.loss_streak = 0
        try:
            from src.monitoring.metrics import set_circuit_breaker_tripped

            set_circuit_breaker_tripped(False)
        except Exception:
            pass
        return True

    def should_pause(self) -> bool:
        if self._manual_tripped:
            return True
        if monitor_breaker.current_state == pybreaker.STATE_OPEN:
            return True
        if (
            self.loss_streak >= self._loss_streak_threshold
            and (time.time() - self.last_loss_ts) < self._loss_streak_cooldown_sec
        ):
            return True
        return False

    def record_trade(self, pnl_usd: float) -> None:
        try:
            from src.strategies.brain_pnl import append_realized_pnl_usd

            append_realized_pnl_usd(float(pnl_usd))
        except Exception:
            pass
        if pnl_usd < -self._loss_usd_threshold:
            self.loss_streak += 1
            self.last_loss_ts = time.time()
            if self.loss_streak >= self._loss_streak_threshold:
                self.trip(f"loss_streak_{self.loss_streak}")
        else:
            self.loss_streak = max(0, self.loss_streak - 1)


circuit_breaker = TradeCircuitBreaker()
