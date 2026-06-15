# src/execution/jito_bundle.py
"""
Jito bundle executor — atomic VersionedTransaction bundles via Block Engine JSON-RPC.

Uses HTTP ``sendBundle`` / ``getBundleStatuses`` (production default). Optionally uses
``jito_searcher_client`` when installed and a searcher keypair is configured.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any

import httpx
from solders.transaction import VersionedTransaction

from src.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _tip_bound(name: str, legacy: str, default: int) -> int:
    raw = (os.getenv(name) or os.getenv(legacy) or str(default)).strip()
    return int(raw)


def _clamp_tip_lamports(tip_lamports: int, settings: Settings | None = None) -> int:
    """Validate tip range; clamp to env bounds or safe default."""
    cfg = settings or get_settings()
    safe_default = int(os.getenv("JITO_TIP_LAMPORTS", str(cfg.JITO_TIP_LAMPORTS)) or 95_000)
    tip = int(tip_lamports)
    if tip < 30_000 or tip > 300_000:
        tip = safe_default
    floor = _tip_bound("JITO_TIP_MIN_LAMPORTS", "JITO_TIP_LAMPORTS_MIN", 45_000)
    cap = _tip_bound("JITO_TIP_MAX_LAMPORTS", "JITO_TIP_LAMPORTS_MAX", 180_000)
    return max(floor, min(cap, tip))


def resolve_dynamic_jito_tip_lamports(
    net_bps: float,
    size_usdc_micro: int,
    *,
    gross_bps: float | None = None,
    max_tip: int | None = None,
    min_tip: int | None = None,
) -> int:
    """Edge-scaled tip via ``src.core.jito_tip`` (profit ratio + live floor cache)."""
    from src.core.jito_tip import resolve_jito_tip_for_trade

    gross = float(gross_bps if gross_bps is not None else net_bps)
    tip = resolve_jito_tip_for_trade(
        float(net_bps),
        gross,
        int(size_usdc_micro),
        log=False,
    )
    cap = max_tip if max_tip is not None else _tip_bound(
        "JITO_TIP_MAX_LAMPORTS", "JITO_TIP_LAMPORTS_MAX", 180_000
    )
    floor = min_tip if min_tip is not None else _tip_bound(
        "JITO_TIP_MIN_LAMPORTS", "JITO_TIP_LAMPORTS_MIN", 25_000
    )
    return max(floor, min(cap, tip))


def _static_jito_tip_lamports(settings: Settings | None = None) -> int:
    """Env-based tip without dynamic scaling."""
    cfg = settings or get_settings()
    base = int(os.getenv("JITO_TIP_LAMPORTS", str(cfg.JITO_TIP_LAMPORTS)))
    high = int(os.getenv("JITO_TIP_LAMPORTS_HIGH", "200000"))
    cex_tip = int(os.getenv("CEX_DEX_JITO_TIP_LAMPORTS", str(cfg.CEX_DEX_JITO_TIP_LAMPORTS)))
    if _env_bool("JITO_TIP_VOLATILITY_MODE", False):
        return max(high, cex_tip, base)
    return max(cex_tip, base)


def resolve_jito_tip_lamports(
    settings: Settings | None = None,
    *,
    net_bps: float | None = None,
    size_usdc_micro: int | None = None,
) -> int:
    """
    Base tip from ``JITO_TIP_LAMPORTS``; ``JITO_TIP_VOLATILITY_MODE`` uses
    ``JITO_TIP_LAMPORTS_HIGH`` (default 200k). CEX-DEX path may override via
    ``CEX_DEX_JITO_TIP_LAMPORTS``.

    When ``JITO_DYNAMIC_TIP=true`` and ``net_bps`` + ``size_usdc_micro`` are set,
    uses ``resolve_dynamic_jito_tip_lamports`` capped at ``JITO_TIP_LAMPORTS_MAX``.
    """
    cfg = settings or get_settings()
    if (
        _env_bool("JITO_DYNAMIC_TIP", True)
        and net_bps is not None
        and size_usdc_micro is not None
        and int(size_usdc_micro) > 0
    ):
        dynamic = resolve_dynamic_jito_tip_lamports(net_bps, int(size_usdc_micro))
        if _env_bool("JITO_DYNAMIC_TIP_USE_MAX_OF_STATIC", False):
            return max(dynamic, _static_jito_tip_lamports(cfg))
        return dynamic
    return _static_jito_tip_lamports(cfg)


def _parse_block_engine_urls() -> list[str]:
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


def _txs_to_base64(txs: list[VersionedTransaction]) -> list[str]:
    return [base64.b64encode(bytes(tx)).decode("ascii") for tx in txs]


class JitoBundleExecutor:
    """Send and track Jito bundles (MEV-protected atomic execution)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.block_engine_urls = _parse_block_engine_urls()
        self.tip_lamports = int(
            os.getenv("JITO_TIP_LAMPORTS", str(self.settings.JITO_TIP_LAMPORTS))
        )
        self.client = httpx.AsyncClient(timeout=12.0)

    async def refresh_tip_floor(self) -> None:
        """Prefetch Jito tip percentiles for dynamic tipping (cached)."""
        if not _env_bool("JITO_TIP_USE_LIVE_FLOOR", True):
            return
        try:
            from src.core.jito_tip import get_current_tip_floor

            floor = await get_current_tip_floor()
            logger.debug("Jito tip floor refreshed | median=%s", floor.get("median"))
        except Exception as exc:
            logger.debug("Jito tip floor refresh skipped: %s", exc)

    async def _send_bundle_http(
        self,
        txs_b64: list[str],
        *,
        tip_lamports: int,
    ) -> dict[str, Any]:
        from src.core.multi_region_jito_sender import (
            MultiRegionJitoSender,
            multi_region_enabled,
            submit_bundle_multi_region,
        )

        tip = _clamp_tip_lamports(tip_lamports, self.settings)
        result = await submit_bundle_multi_region(
            txs_b64,
            tip_lamports=tip,
            client=self.client,
        )
        if result.get("success"):
            return result

        if multi_region_enabled():
            logger.info("Jito parallel regions failed — trying sequential fallback")
            sender = MultiRegionJitoSender(client=self.client)
            result = await sender.send_bundle_b64_sequential(txs_b64, tip_lamports=tip)

        if not result.get("success"):
            logger.info("Jito all regions failed → RPC fallback recommended")
        return result

    async def send_bundle(
        self,
        txs: list[VersionedTransaction],
        priority_fee: int | None = None,
    ) -> bool:
        """Send atomic Jito bundle from signed ``VersionedTransaction`` objects."""
        tip = _clamp_tip_lamports(
            int(priority_fee if priority_fee is not None else self.tip_lamports),
            self.settings,
        )

        if self.settings.test_mode or self.settings.simulate:
            logger.info("SIMULATE bundle with %s txs | tip=%s", len(txs), tip)
            return True

        if not txs:
            logger.error("send_bundle called with empty tx list")
            return False

        txs_b64 = _txs_to_base64(txs)
        result = await self._send_bundle_http(txs_b64, tip_lamports=tip)
        if not result.get("success"):
            if _env_bool("JITO_RPC_FALLBACK_ON_FAIL", True):
                logger.info("Jito send_bundle failed — caller should use RPC fallback")
            return False
        if _env_bool("JITO_AWAIT_BUNDLE_POLL", True) and result.get("bundle_id"):
            return await self.await_bundle_landed(str(result["bundle_id"]))
        return True

    async def await_bundle_landed(
        self,
        bundle_id: str,
        *,
        timeout_sec: float | None = None,
    ) -> bool:
        """Poll ``getBundleStatuses`` until landed or timeout."""
        timeout = float(
            timeout_sec
            if timeout_sec is not None
            else os.getenv("JITO_BUNDLE_POLL_TIMEOUT_SEC", "45")
        )
        interval = float(os.getenv("JITO_BUNDLE_POLL_INTERVAL_SEC", "0.5"))
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            status = await self.get_bundle_status(bundle_id)
            if status.get("landed"):
                logger.info("Jito bundle landed | id=%s", bundle_id)
                return True
            st = str(status.get("status", "")).lower()
            if st in ("failed", "rejected", "dropped"):
                logger.warning("Jito bundle terminal status=%s id=%s", st, bundle_id)
                return False
            await asyncio.sleep(interval)

        logger.warning("Jito bundle poll timeout | id=%s", bundle_id)
        return False

    async def send_bundle_b64(
        self,
        txs_b64: list[str],
        *,
        tip_lamports: int | None = None,
    ) -> dict[str, Any]:
        """Send pre-encoded base64 transactions (Jupiter swap tx string)."""
        tip = _clamp_tip_lamports(
            int(
                tip_lamports
                if tip_lamports is not None
                else resolve_jito_tip_lamports(self.settings)
            ),
            self.settings,
        )

        if self.settings.test_mode or self.settings.simulate:
            logger.info("SIMULATE bundle (b64) | txs=%s tip=%s", len(txs_b64), tip)
            return {"success": True, "bundle_id": "simulated_jito_tx", "txid": "simulated_jito_tx"}

        if not txs_b64:
            return {"success": False, "error": "empty_bundle"}

        await self.refresh_tip_floor()

        for attempt in range(3):
            result = await self._send_bundle_http(txs_b64, tip_lamports=tip)
            if result.get("total_regions"):
                logger.info(
                    "Multi-region send: %s/%s regions accepted",
                    result.get("success_count", 0),
                    result.get("total_regions", 0),
                )
            if not result.get("success"):
                await asyncio.sleep(0.8 * (attempt + 1))
                continue

            result.setdefault("txid", result.get("bundle_id"))
            bundle_id = result.get("bundle_id")
            if _env_bool("JITO_AWAIT_BUNDLE_POLL", True) and bundle_id:
                landed = await self.await_bundle_landed(str(bundle_id))
                result["landed"] = landed
                result["success"] = landed
                if landed:
                    return result
                logger.warning(
                    "Jito bundle not landed (attempt %s) id=%s",
                    attempt + 1,
                    bundle_id,
                )
            else:
                return result
            await asyncio.sleep(0.8 * (attempt + 1))

        return {"success": False, "error": "jito_send_failed"}

    async def send_bundle_for_simulation(
        self,
        transactions: list[VersionedTransaction],
        tip_lamports: int = 150_000,
    ) -> str:
        """
        Dry-run path for ``BundleSimulator`` — returns a pseudo bundle id without landing.
        """
        if self.settings.test_mode or self.settings.simulate:
            return "sim-bundle-id"
        _ = tip_lamports
        return f"dryrun-{_txs_to_base64(transactions)[0][:16]}"

    async def get_bundle_status(self, bundle_id: str) -> dict[str, Any]:
        """Poll bundle landing status via Block Engine JSON-RPC."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBundleStatuses",
            "params": [[bundle_id]],
        }
        for endpoint in self.block_engine_urls:
            try:
                url = f"{endpoint.rstrip('/')}/api/v1/bundles"
                resp = await self.client.post(url, json=payload)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                result = data.get("result") or {}
                value = result.get("value") or []
                if value:
                    entry = value[0] if isinstance(value, list) else value
                    status = (
                        entry.get("confirmation_status")
                        or entry.get("status")
                        or "unknown"
                    )
                    landed = status in ("confirmed", "finalized", "landed", "Landed")
                    return {
                        "success": landed,
                        "landed": landed,
                        "status": status,
                        "raw": entry,
                    }
                return {"success": False, "status": "pending", "landed": False}
            except Exception as exc:
                logger.debug("get_bundle_status %s: %s", endpoint, exc)
        return {"success": False, "status": "unknown", "landed": False}

    async def backrun_opportunity(
        self,
        executor: Any,
        quote1: dict[str, Any],
        quote2: dict[str, Any],
        quote3: dict[str, Any],
        detected_amount: int,
    ) -> str | None:
        """Build a 3-leg Jupiter swap bundle (USDC→mid→SOL→USDC) and submit via Jito."""
        _ = detected_amount
        if self.settings.test_mode or self.settings.simulate:
            logger.info("[SIMULATE] Helius 3-leg backrun bundle")
            return "sim-helius-backrun"

        wallet = (
            self.settings.wallet_pubkey
            or self.settings.WALLET_PUBKEY
            or os.getenv("WALLET_PUBKEY", "")
        )
        if not wallet:
            logger.error("backrun_opportunity: WALLET_PUBKEY not set")
            return None

        slippage = int(os.getenv("MAX_SLIPPAGE_BPS", str(self.settings.MAX_SLIPPAGE_BPS)))
        txs_b64: list[str] = []
        for quote in (quote1, quote2, quote3):
            if not quote or "outAmount" not in quote:
                logger.debug("backrun_opportunity: missing quote leg")
                return None
            swap_data = await executor.build_swap_transaction(
                {"quote": quote},
                str(wallet),
                slippage_bps=slippage,
            )
            if not swap_data or "swapTransaction" not in swap_data:
                logger.warning("backrun_opportunity: swap build failed for one leg")
                return None
            txs_b64.append(swap_data["swapTransaction"])

        tip = int(os.getenv("CEX_DEX_JITO_TIP_LAMPORTS", str(self.settings.CEX_DEX_JITO_TIP_LAMPORTS)))
        result = await self.send_bundle_b64(txs_b64, tip_lamports=tip)
        if result.get("success"):
            return str(result.get("bundle_id") or result.get("txid") or "")
        logger.warning("backrun_opportunity: Jito send failed: %s", result.get("error"))
        return None

    async def close(self) -> None:
        await self.client.aclose()


_jito_bundle_executor: JitoBundleExecutor | None = None


def get_jito_bundle_executor(settings: Settings | None = None) -> JitoBundleExecutor:
    global _jito_bundle_executor
    if _jito_bundle_executor is None:
        _jito_bundle_executor = JitoBundleExecutor(settings)
    return _jito_bundle_executor


async def send_jito_bundle_b64(
    signed_transaction: str,
    tip_lamports: int | None = None,
) -> dict[str, Any]:
    """High-level helper used by Jupiter / strategy modules."""
    executor = get_jito_bundle_executor()
    return await executor.send_bundle_b64([signed_transaction], tip_lamports=tip_lamports)


async def close_jito_bundle_executor() -> None:
    global _jito_bundle_executor
    if _jito_bundle_executor is not None:
        await _jito_bundle_executor.close()
        _jito_bundle_executor = None
