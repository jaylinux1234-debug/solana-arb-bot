import logging
from collections.abc import Callable

from solana.rpc.websocket_api import connect

from src.config.settings import settings

logger = logging.getLogger(__name__)


class HeliusSubscriber:
    def __init__(self):
        self.ws_url = settings.SOLANA_RPC_WS_URL
        if not self.ws_url:
            raise ValueError("SOLANA_RPC_WS_URL not configured in .env")

    async def subscribe_to_blocks(self, callback: Callable):
        """Real-time block subscription (replaces polling)"""
        logger.info(f"Subscribing to blocks via Helius WS: {self.ws_url[:50]}...")
        
        async with connect(self.ws_url) as ws:
            # Subscribe to blocks
            await ws.block_subscribe(
                commitment="confirmed",
                max_supported_transaction_version=0
            )
            
            first_resp = await ws.recv()
            logger.info(f"Block subscription active: {first_resp}")
            
            async for msg in ws:
                try:
                    if msg and hasattr(msg, 'result') and isinstance(msg.result, dict):
                        await callback(msg.result)
                except Exception as e:
                    logger.warning(f"Error in block callback: {e}")

    async def subscribe_program_logs(self, program_id: str, callback: Callable):
        """Subscribe to specific program logs (Jupiter, Kamino, etc.)"""
        async with connect(self.ws_url) as ws:
            await ws.logs_subscribe(
                {"mentions": [program_id]},
                commitment="confirmed"
            )
            async for msg in ws:
                await callback(msg)