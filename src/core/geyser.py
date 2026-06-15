# src/core/geyser.py — Yellowstone / Geyser account stream (optional low-latency oracle path).

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from solders.pubkey import Pubkey

from src.config.settings import settings

logger = logging.getLogger(__name__)

# Override via GEYSER_POOL_KEYS=comma-separated base58 pubkeys (Raydium/Orca/Kamino reserves).
DEFAULT_GEYSER_POOL_KEYS: list[str] = []


def default_geyser_pool_keys() -> list[Pubkey]:
    raw = (os.getenv("GEYSER_POOL_KEYS") or "").strip()
    if not raw:
        keys = list(DEFAULT_GEYSER_POOL_KEYS)
        if settings.KAMINO_USDC_DEBT_RESERVE:
            keys.append(settings.KAMINO_USDC_DEBT_RESERVE)
        return [Pubkey.from_string(k) for k in keys if k]

    out: list[Pubkey] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(Pubkey.from_string(part))
    return out


class GeyserClient:
    """Subscribe to on-chain pool/reserve accounts for faster DEX oracle updates."""

    def __init__(
        self,
        *,
        url: str | None = None,
        token: str | None = None,
    ) -> None:
        self.url = (url or settings.YELLOWSTONE_GRPC_URL or "").strip()
        self.token = (
            token
            or settings.GEYSER_TOKEN
            or settings.GEYSER_GRPC_TOKEN
            or settings.YELLOWSTONE_GRPC_AUTH
            or ""
        ).strip()
        self._channel: Any = None
        self._stub: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def _connect(self) -> bool:
        if not self.enabled:
            return False
        try:
            import grpc
        except ImportError:
            logger.warning(
                "Geyser: grpcio not installed — pip install grpcio. Subscription disabled."
            )
            return False

        target = self.url.replace("grpc://", "").replace("grpcs://", "")
        if self.url.startswith("grpcs://"):
            self._channel = grpc.aio.secure_channel(target, grpc.ssl_channel_credentials())
        else:
            self._channel = grpc.aio.insecure_channel(target)

        # Yellowstone gRPC stub wiring is provider-specific; keep stream hook for later.
        self._stub = None
        logger.info("Geyser: channel ready for %s (stub TBD — using poll fallback)", self.url)
        return True

    async def subscribe_pools(
        self,
        pool_keys: list[Pubkey],
        callback: Callable[[Any], Awaitable[None]],
    ) -> None:
        """
        Stream account updates for ``pool_keys`` and invoke ``callback`` per message.

        When the Yellowstone stub is not wired, logs once and idles until cancelled.
        """
        if not self.enabled:
            logger.info("Geyser: YELLOWSTONE_GRPC_URL unset — subscription skipped")
            return

        if not pool_keys:
            logger.warning("Geyser: no pool keys configured (set GEYSER_POOL_KEYS)")
            return

        connected = await self._connect()
        if not connected:
            return

        labels = [str(k)[:8] + "…" for k in pool_keys[:5]]
        logger.info(
            "Geyser: subscribing to %s pool(s) %s%s",
            len(pool_keys),
            labels,
            " …" if len(pool_keys) > 5 else "",
        )

        # Placeholder until yellowstone proto SubscribeRequest is integrated.
        if self._stub is None:
            logger.warning(
                "Geyser: Yellowstone subscribe stub not configured — "
                "account stream inactive (wire geyser.proto + stub)"
            )
            try:
                while True:
                    await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                logger.info("Geyser: subscription task cancelled")
            return

        request = {
            "accounts": {str(k): {"account": [str(k)]} for k in pool_keys},
            "commitment": "processed",
        }
        async for update in self._stub.subscribe(request):
            await callback(update)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None


async def on_pool_update_default(update: Any) -> None:
    """Default handler: debug log only (extend to parse reserves → dex mid)."""
    logger.debug("Geyser pool update received: %s", type(update).__name__)
