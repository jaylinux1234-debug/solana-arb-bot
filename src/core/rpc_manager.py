# src/core/rpc_manager.py
"""
Multi-provider Solana RPC manager — Helius primary + QuickNode/fast secondary.

Prefers ``SOLANA_RPC_URL_FAST`` for sends and hot-path reads; falls back to the
full weighted chain in ``src.core.rpc_config`` on 429/errors.
"""

from __future__ import annotations

import logging
from typing import Any

from src.config.settings import get_settings
from src.core.rpc_config import (
    call_with_rpc_fallback,
    filtered_rpc_fallback_chain,
    get_upgraded_robust_provider,
    mark_rpc_rate_limited,
    rpc_provider_label,
)

logger = logging.getLogger(__name__)


def _mask_rpc(url: str) -> str:
    if not url:
        return ""
    if "api-key=" in url:
        return url.split("api-key=")[0] + "api-key=***"
    if ".quiknode.pro/" in url:
        return url.rsplit("/", 1)[0] + "/***"
    return url[:48] + ("..." if len(url) > 48 else "")


class RpcManager:
    """Singleton RPC coordinator for v2 strategies."""

    _instance: RpcManager | None = None

    def __new__(cls) -> RpcManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return

        settings = get_settings()
        self.primary = (
            getattr(settings, "SOLANA_RPC_URL", None)
            or getattr(settings, "solana_rpc_url", "")
            or ""
        ).strip()
        fast = (
            getattr(settings, "SOLANA_RPC_URL_FAST", None)
            or getattr(settings, "solana_rpc_url_fast", None)
            or ""
        ).strip()
        self.fast = fast or self.primary

        self._provider = get_upgraded_robust_provider()
        self.providers = list(self._provider.providers.keys()) or ["helius_primary"]
        # "fast" lane → QuickNode; "chain" → full fallback rotation
        self.current = "fast" if self.fast and self.fast != self.primary else "chain"
        self._initialized = True

    async def initialize(self) -> None:
        """Warm provider list and log primary/fast endpoints."""
        chain = filtered_rpc_fallback_chain("default")
        if chain:
            self.current = (
                "fast"
                if self.fast in chain and not chain[0] == self.primary
                else rpc_provider_label(chain[0])
            )
        logger.info(
            "RPC manager ready | primary=%s fast=%s lane=%s providers=%s",
            _mask_rpc(self.primary),
            _mask_rpc(self.fast),
            self.current,
            self.providers,
        )

    async def get_best_rpc(self) -> str:
        """Return preferred endpoint — fast (QuickNode) when the fast lane is active."""
        if self.fast and self.current in ("fast", "primary", "helius_fast"):
            return self.fast
        return self.primary or self.fast

    def http_chain(self, purpose: str = "default") -> list[str]:
        return filtered_rpc_fallback_chain(purpose)

    async def call(self, purpose: str, fn: Any, *, label: str = "rpc") -> Any:
        """Run ``fn(rpc_url)`` across the fallback chain."""
        return await call_with_rpc_fallback(purpose, fn, label=label)

    async def send_transaction(
        self,
        tx: Any,
        *,
        commitment: str = "confirmed",
        skip_preflight: bool = False,
    ) -> str:
        """Send a signed versioned transaction — fast RPC first, then fallback chain."""
        from solana.rpc.async_api import AsyncClient
        from solana.rpc.types import TxOpts
        from solders.transaction import VersionedTransaction

        if not isinstance(tx, VersionedTransaction):
            raise TypeError("send_transaction expects a signed VersionedTransaction")

        raw = bytes(tx)

        async def _send(rpc_url: str) -> str:
            async with AsyncClient(rpc_url) as client:
                resp = await client.send_raw_transaction(
                    raw,
                    opts=TxOpts(
                        skip_preflight=skip_preflight,
                        preflight_commitment=commitment,
                        max_retries=3,
                    ),
                )
                return str(resp.value)

        preferred = await self.get_best_rpc()
        if preferred:
            try:
                sig = await _send(preferred)
                logger.info(
                    "RPC tx sent | sig=%s via=fast url=%s",
                    sig[:12],
                    _mask_rpc(preferred),
                )
                return sig
            except Exception as exc:
                logger.warning("Fast RPC send failed (%s) — falling back to chain", exc)
                self.record_failure(provider="helius_fast")

        try:
            sig = await self.call("transaction", _send, label="send_transaction")
            logger.info("RPC tx sent | sig=%s provider=%s", sig[:12], self.current)
            return sig
        except Exception as exc:
            logger.error("RPC send failed: %s", exc)
            self.record_failure()
            raise

    def record_failure(self, provider: str | None = None) -> None:
        name = provider or self.current
        logger.warning("RPC failure on %s — rotating", name)

        if self.current == "fast":
            self.current = "chain"
            logger.info("RPC lane switch | fast → fallback chain")
            return

        urls = self._provider.http_chain("default")
        for url in urls:
            if rpc_provider_label(url) == name or name in url:
                mark_rpc_rate_limited(url)
                break

        if name == "helius_primary" or "helius" in str(name):
            if self.fast and self.fast != self.primary:
                self.current = "fast"
                logger.info("RPC lane switch | chain → fast")
                return

        idx = self.providers.index(name) if name in self.providers else -1
        if idx >= 0 and len(self.providers) > 1:
            self.current = self.providers[(idx + 1) % len(self.providers)]
        elif len(self.providers) > 1:
            self.current = self.providers[1]


def get_rpc_manager() -> RpcManager:
    return RpcManager()
