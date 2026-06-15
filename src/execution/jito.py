# src/execution/jito.py
"""Jito bundle facade — re-exports ``JitoBundleExecutor`` and legacy helpers."""

from __future__ import annotations

import logging
from typing import Any

from solders.transaction import VersionedTransaction

from src.config.settings import get_settings, settings
from src.execution.jito_bundle import (
    JitoBundleExecutor,
    get_jito_bundle_executor,
    send_jito_bundle_b64,
)

logger = logging.getLogger(__name__)

# Legacy alias used by helius / bundle_simulator
JitoClient = JitoBundleExecutor


def get_jito_client() -> JitoBundleExecutor:
    return get_jito_bundle_executor()


async def send_jito_bundle(
    signed_transaction: str,
    tip_lamports: int | None = None,
    *,
    profit_usdc: float | None = None,
) -> dict[str, Any]:
    """Send a single base64-encoded transaction as a Jito bundle."""
    if tip_lamports is None:
        if profit_usdc is not None:
            from src.core.jito_tip import compute_tip_from_profit_usdc

            tip_lamports = compute_tip_from_profit_usdc(float(profit_usdc))
        else:
            tip_lamports = settings.JITO_TIP_LAMPORTS
    return await send_jito_bundle_with_retry(
        [signed_transaction],
        tip_lamports=tip_lamports,
    )


async def send_jito_bundle_with_retry(
    txs_b64: list[str],
    *,
    tip_lamports: int | None = None,
    profit_usdc: float | None = None,
) -> dict[str, Any]:
    """Multi-region Jito send with dynamic tip and per-region retry."""
    import asyncio

    from src.core.jito_tip import compute_tip_from_profit_usdc

    tip = int(
        tip_lamports
        if tip_lamports is not None
        else (
            compute_tip_from_profit_usdc(float(profit_usdc))
            if profit_usdc is not None
            else settings.JITO_TIP_LAMPORTS
        )
    )

    executor = get_jito_bundle_executor()
    last_error: str | None = None
    regions = executor.block_engine_urls or ["https://ny.mainnet.block-engine.jito.wtf"]

    for region_url in regions:
        region = region_url.split(".")[0].replace("https://", "")
        try:
            result = await executor.send_bundle_b64(txs_b64, tip_lamports=tip)
            if result.get("success"):
                result.setdefault("tip_lamports", tip)
                result.setdefault("region", region)
                return result
            last_error = str(result.get("error") or "send_failed")
            logger.warning("Jito %s failed: %s", region, last_error)
        except Exception as exc:
            last_error = str(exc)
            logger.warning("Jito %s failed: %s", region, exc)
        await asyncio.sleep(0.3)

    logger.error("All Jito regions failed | last=%s", last_error)
    return {"success": False, "error": last_error or "all_jito_regions_failed", "tip_lamports": tip}


async def close_jito_client() -> None:
    from src.execution.jito_bundle import close_jito_bundle_executor

    await close_jito_bundle_executor()


# Stubs for modules that import richer Jito helpers (optional integrations)
def configure_jito(*_args: Any, **_kwargs: Any) -> None:
    logger.debug("configure_jito: no-op (use JitoBundleExecutor + env JITO_TIP_LAMPORTS)")


class JitoMultiRelay:
    """Minimal relay wrapper for strategy code paths."""

    def __init__(self, client: Any = None, keypair: Any = None) -> None:
        self.client = client
        self.keypair = keypair
        self._executor = get_jito_bundle_executor()

    async def send_bundle(self, txs: list[VersionedTransaction], tip_lamports: int = 0) -> bool:
        tip = tip_lamports or settings.JITO_TIP_LAMPORTS
        return await self._executor.send_bundle(txs, priority_fee=tip)


async def send_jito_bundle_multi_relay(
    txs: list[VersionedTransaction],
    *,
    tip_lamports: int | None = None,
) -> str | None:
    executor = get_jito_bundle_executor()
    ok = await executor.send_bundle(txs, priority_fee=tip_lamports)
    return "bundle-sent" if ok else None


async def await_jito_bundle_poll(bundle_id: str, timeout_sec: float = 30.0) -> dict[str, Any]:
    executor = get_jito_bundle_executor()
    elapsed = 0.0
    while elapsed < timeout_sec:
        status = await executor.get_bundle_status(bundle_id)
        if status.get("landed"):
            return status
        import asyncio

        await asyncio.sleep(1.0)
        elapsed += 1.0
    return {"success": False, "status": "timeout", "landed": False}


class JitoHelper:
    def __init__(self, client: Any, keypair: Any) -> None:
        self.client = client
        self.keypair = keypair
        self._executor = get_jito_bundle_executor()


async def create_jito_helper(client: Any, keypair: Any) -> JitoHelper:
    return JitoHelper(client, keypair)
