import logging
import os
from typing import Any

from solders.transaction import VersionedTransaction

from src.config.settings import settings

logger = logging.getLogger(__name__)

try:
    from jito_searcher_client import JitoClient  # type: ignore[import-not-found]
except ImportError:
    logger.warning("jito-searcher-client not installed. Install with: pip install jito-searcher-client")
    JitoClient = None


class JitoExecutor:
    def __init__(self):
        self.client: Any = None
        self.tip_lamports = int(os.getenv("JITO_TIP_LAMPORTS", "100000"))
        self.block_engines = [
            url.strip()
            for url in os.getenv(
                "JITO_BLOCK_ENGINE_URLS",
                "ny.mainnet.block-engine.jito.wtf",
            ).split(",")
            if url.strip()
        ]

    async def ensure_client(self):
        if not self.client and JitoClient is not None:
            self.client = JitoClient(
                block_engine_url=self.block_engines[0] if self.block_engines else None,
                tip_lamports=self.tip_lamports,
            )

    async def send_bundle(self, transactions: list[VersionedTransaction]) -> str | None:
        """Send transactions as Jito Bundle."""
        if JitoClient is None:
            logger.error("Jito client not available")
            return None

        await self.ensure_client()
        if self.client is None:
            logger.error("Jito client initialization failed")
            return None

        try:
            if hasattr(self.client, "build_bundle"):
                bundle = await self.client.build_bundle(transactions)
            else:
                bundle = transactions

            result = await self.client.send_bundle(bundle)
            bundle_id = getattr(result, "bundle_id", None) or getattr(result, "value", None)
            if bundle_id:
                logger.info("✅ Jito Bundle sent! Bundle ID: %s", bundle_id)
                return str(bundle_id)
            logger.warning("Jito bundle returned no bundle id")
            return None
        except Exception as exc:
            logger.error("Jito bundle failed: %s", exc)
            return None

    async def simulate_and_send(self, tx: VersionedTransaction) -> bool:
        """Simulate first, then send via Jito if successful."""
        from solana.rpc.async_api import AsyncClient

        client = AsyncClient(settings.solana_rpc_url)

        sim_result = await client.simulate_transaction(tx)

        if sim_result.value.err:
            logger.warning("Simulation failed: %s", sim_result.value.err)
            return False

        logger.info("✅ Simulation passed. Sending via Jito...")
        bundle_id = await self.send_bundle([tx])
        return bundle_id is not None