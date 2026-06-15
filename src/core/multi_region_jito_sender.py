"""Parallel Jito block-engine submission across regions (first success wins)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def parse_block_engine_urls() -> list[str]:
    raw = os.getenv(
        "JITO_BLOCK_ENGINE_URLS",
        "ny.mainnet.block-engine.jito.wtf,amsterdam.mainnet.block-engine.jito.wtf",
    )
    urls: list[str] = []
    for part in raw.split(","):
        host = part.strip()
        if not host:
            continue
        if host.startswith("http"):
            urls.append(host.rstrip("/"))
        else:
            urls.append(f"https://{host}")
    return urls or ["https://ny.mainnet.block-engine.jito.wtf"]


def multi_region_enabled() -> bool:
    return _env_bool("JITO_SUBMIT_MULTI_REGION", False)


def _normalize_bundle_bytes(bundle_bytes: str | list[str]) -> list[str]:
    if isinstance(bundle_bytes, str):
        return [bundle_bytes]
    return list(bundle_bytes)


class MultiRegionJitoSender:
    """Submit the same bundle to multiple Jito regions concurrently."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        endpoints: list[str] | None = None,
        timeout_sec: float = 12.0,
    ) -> None:
        self.endpoints = endpoints if endpoints is not None else parse_block_engine_urls()
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_sec)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _bundle_payload(self, txs_b64: list[str]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [txs_b64, {"encoding": "base64"}],
        }

    async def _post_bundle(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            url = f"{endpoint.rstrip('/')}/api/v1/bundles"
            resp = await self.client.post(url, json=payload)
            if resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"http_{resp.status_code}",
                    "endpoint": endpoint,
                }
            data = resp.json()
            if "error" in data:
                return {
                    "success": False,
                    "error": str(data["error"]),
                    "endpoint": endpoint,
                }
            bundle_id = data.get("result")
            if bundle_id:
                return {
                    "success": True,
                    "bundle_id": bundle_id,
                    "endpoint": endpoint,
                }
        except Exception as exc:
            return {"success": False, "error": str(exc), "endpoint": endpoint}
        return {"success": False, "error": "no_bundle_id", "endpoint": endpoint}

    async def send_bundle_b64(
        self,
        txs_b64: list[str],
        *,
        tip_lamports: int = 0,
    ) -> dict[str, Any]:
        """
        POST ``sendBundle`` to all configured regions in parallel.

        Returns first successful ``{success, bundle_id, endpoint}`` or an error dict.
        """
        if not txs_b64:
            return {"success": False, "error": "empty_bundle"}

        payload = self._bundle_payload(txs_b64)
        tasks = [asyncio.create_task(self._post_bundle(ep, payload)) for ep in self.endpoints]
        try:
            for done in asyncio.as_completed(tasks):
                result = await done
                if result.get("success"):
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    logger.info(
                        "Jito multi-region bundle sent | id=%s tip=%s endpoint=%s",
                        result.get("bundle_id"),
                        tip_lamports,
                        result.get("endpoint"),
                    )
                    return result
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

        return {"success": False, "error": "all_regions_failed"}

    async def send_bundle_b64_all_regions(
        self,
        txs_b64: list[str],
        *,
        tip_lamports: int = 0,
    ) -> dict[str, Any]:
        """Fan out to every region and report how many accepted the bundle."""
        if not txs_b64:
            return {
                "success": False,
                "error": "empty_bundle",
                "success_count": 0,
                "total_regions": len(self.endpoints),
            }

        payload = self._bundle_payload(txs_b64)
        results = await asyncio.gather(
            *[self._post_bundle(ep, payload) for ep in self.endpoints],
            return_exceptions=False,
        )
        success_count = sum(1 for r in results if r.get("success"))
        total_regions = len(self.endpoints)
        winner = next((r for r in results if r.get("success")), None)

        if winner:
            logger.info(
                "Jito multi-region bundle sent | id=%s tip=%s endpoint=%s accepted=%s/%s",
                winner.get("bundle_id"),
                tip_lamports,
                winner.get("endpoint"),
                success_count,
                total_regions,
            )
            return {
                "success": True,
                "bundle_id": winner.get("bundle_id"),
                "endpoint": winner.get("endpoint"),
                "success_count": success_count,
                "total_regions": total_regions,
                "region_results": results,
            }

        return {
            "success": False,
            "error": "all_regions_failed",
            "success_count": 0,
            "total_regions": total_regions,
            "region_results": results,
        }

    async def send_bundle_b64_sequential(
        self,
        txs_b64: list[str],
        *,
        tip_lamports: int = 0,
    ) -> dict[str, Any]:
        """Fallback: try regions one-by-one (legacy behavior)."""
        if not txs_b64:
            return {
                "success": False,
                "error": "empty_bundle",
                "success_count": 0,
                "total_regions": len(self.endpoints),
            }

        payload = self._bundle_payload(txs_b64)
        last_error = "all_endpoints_failed"
        for endpoint in self.endpoints:
            result = await self._post_bundle(endpoint, payload)
            if result.get("success"):
                logger.info(
                    "Jito bundle sent | id=%s tip=%s endpoint=%s",
                    result.get("bundle_id"),
                    tip_lamports,
                    endpoint,
                )
                return {
                    "success": True,
                    "bundle_id": result.get("bundle_id"),
                    "endpoint": endpoint,
                    "success_count": 1,
                    "total_regions": 1,
                }
            last_error = str(result.get("error", last_error))
            logger.warning("Jito endpoint %s failed: %s", endpoint, last_error)
        return {
            "success": False,
            "error": last_error,
            "success_count": 0,
            "total_regions": len(self.endpoints),
        }


async def send_bundle_multi_region(
    bundle_bytes: str | list[str],
    *,
    tip_lamports: int = 0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """
    Submit a signed bundle (base64 tx or list) to Jito block engines.

    When ``JITO_SUBMIT_MULTI_REGION=true``, fans out to all regions and returns
    ``success_count`` / ``total_regions``.
    """
    txs_b64 = _normalize_bundle_bytes(bundle_bytes)
    sender = MultiRegionJitoSender(client=client)
    try:
        if multi_region_enabled():
            return await sender.send_bundle_b64_all_regions(txs_b64, tip_lamports=tip_lamports)
        return await sender.send_bundle_b64_sequential(txs_b64, tip_lamports=tip_lamports)
    finally:
        if client is None:
            await sender.close()


async def submit_bundle_multi_region(
    txs_b64: list[str],
    *,
    tip_lamports: int = 0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Module-level helper used by ``JitoBundleExecutor``."""
    return await send_bundle_multi_region(
        txs_b64,
        tip_lamports=tip_lamports,
        client=client,
    )
