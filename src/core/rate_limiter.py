"""Async token-bucket rate limiter for RPC / WS calls."""

from __future__ import annotations

import asyncio
import os
import time


class TokenBucket:
    def __init__(self, rate: float = 10, capacity: float = 20) -> None:
        self.rate = float(rate)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.last_refill = time.time()

    async def acquire(self) -> None:
        while True:
            now = time.time()
            self.tokens = min(
                self.capacity,
                self.tokens + (now - self.last_refill) * self.rate,
            )
            self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep(0.05)


_rpc_bucket: TokenBucket | None = None


def get_rpc_rate_limiter() -> TokenBucket:
    """Singleton bucket from ``RPC_RATE_PER_SEC`` / ``RPC_RATE_CAPACITY`` env."""
    global _rpc_bucket
    if _rpc_bucket is None:
        rate = float(os.getenv("RPC_RATE_PER_SEC", "8"))
        cap_raw = os.getenv("RPC_RATE_CAPACITY", "")
        capacity = float(cap_raw) if cap_raw.strip() else max(rate * 2, rate)
        _rpc_bucket = TokenBucket(rate=rate, capacity=capacity)
    return _rpc_bucket
