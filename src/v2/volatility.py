"""Short-horizon CEX bid volatility for v2 adaptive gates."""

from __future__ import annotations

import statistics
import time
from collections import deque


class VolatilityTracker:
    """Rolling SOL bid volatility (return stdev in %)."""

    def __init__(self, lookback_min: int = 5, *, sample_interval_sec: float = 4.0) -> None:
        samples = max(12, int(lookback_min * 60 / max(sample_interval_sec, 1.0)))
        self.prices: deque[float] = deque(maxlen=samples)
        self.last_update = 0.0
        self.sample_interval_sec = sample_interval_sec

    def update(self, price: float) -> None:
        if price <= 0:
            return
        now = time.time()
        if now - self.last_update >= self.sample_interval_sec:
            self.prices.append(float(price))
            self.last_update = now

    def get_volatility_pct(self) -> float:
        if len(self.prices) < 10:
            return 0.8
        returns = [
            100.0 * (self.prices[i] - self.prices[i - 1]) / self.prices[i - 1]
            for i in range(1, len(self.prices))
            if self.prices[i - 1] > 0
        ]
        if len(returns) < 2:
            return 0.8
        return float(statistics.stdev(returns))
