"""
Kamino Finance public REST helpers (Buildkit / 2026).

Base URL: https://api.kamino.finance
Reference: https://kamino.com/docs/llms.txt

Common Klend V2 paths used by the liquidation monitor:
  GET /v2/kamino-market
  GET /v2/kamino-market/{lendingMarketPubkey}/obligations-with-open-borrow-orders
  GET /v2/kamino-market/{lendingMarketPubkey}/obligations/{obligationPubkey}/metrics/history
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = os.getenv("KAMINO_API_BASE", "https://api.kamino.finance").rstrip("/")
DEFAULT_ENV = os.getenv("KAMINO_API_ENV", "mainnet-beta")
API_VERSION = os.getenv("KAMINO_API_VERSION", "v2").strip("/") or "v2"


class KaminoAPI:
    """Thin URL + fetch layer for Kamino public HTTP API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        env: str = DEFAULT_ENV,
        api_version: str = API_VERSION,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.env = env
        self.api_version = api_version.strip("/") or "v2"

    def _v2_market_path(self, lending_market_pk: str, *segments: str) -> str:
        tail = "/".join([s.strip("/") for s in segments])
        prefix = f"{self.base_url}/{self.api_version}/kamino-market/{lending_market_pk}"
        return f"{prefix}/{tail}" if tail else prefix

    @staticmethod
    async def safe_json(resp: aiohttp.ClientResponse) -> Any:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if resp.status != 200:
            raw = await resp.text()
            # Keep logs readable when upstream returns large HTML/cloudflare pages.
            snippet = " ".join(raw.split())[:180]
            if resp.status in (429, 500, 502, 503, 504):
                logger.warning("Kamino API transient HTTP %s: %s", resp.status, snippet)
            else:
                logger.warning("Kamino API HTTP %s: %s", resp.status, snippet)
            return None
        if "application/json" not in ctype:
            raw = await resp.text()
            snippet = " ".join(raw.split())[:180]
            logger.warning("Kamino API non-JSON response (%s): %s", ctype, snippet)
            return None
        return await resp.json()

    async def fetch_markets(self, session: aiohttp.ClientSession) -> list[dict[str, Any]]:
        """GET /{version}/kamino-market — list lending markets (names, lendingMarket, lookupTable)."""
        url = f"{self.base_url}/{self.api_version}/kamino-market"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            data = await KaminoAPI.safe_json(resp)
        if isinstance(data, list):
            return data
        return []

    def primary_lending_market(self, markets: list[dict[str, Any]]) -> str | None:
        for row in markets:
            if row.get("isPrimary") and row.get("lendingMarket"):
                return str(row["lendingMarket"])
        return None

    def obligations_open_borrow_orders_url(self, lending_market_pk: str) -> str:
        q = urlencode({"env": self.env})
        return (
            f"{self._v2_market_path(lending_market_pk, 'obligations-with-open-borrow-orders')}?{q}"
        )

    def obligation_metrics_history_url(
        self,
        lending_market_pk: str,
        obligation_pk: str,
        *,
        use_stake_rate_for_obligation: bool = False,
    ) -> str:
        params: dict[str, Any] = {"env": self.env}
        if use_stake_rate_for_obligation:
            params["useStakeRateForObligation"] = "true"
        q = urlencode(params)
        return (
            f"{self._v2_market_path(lending_market_pk, 'obligations', obligation_pk, 'metrics', 'history')}"
            f"?{q}"
        )
