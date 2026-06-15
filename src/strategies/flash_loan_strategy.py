import asyncio
import logging
from typing import Any

from src.cex.backpack_client import BackpackClient
from src.config.settings import settings
from src.core.price_oracle import PriceOracle
from src.dex.jupiter import JupiterExecutor
from src.execution.jito_executor import JitoExecutor

logger = logging.getLogger(__name__)


class FlashLoanStrategy:
    def __init__(self):
        self.jito = JitoExecutor()
        self.cex_client = BackpackClient()
        self.jupiter = JupiterExecutor()
        self.oracle = PriceOracle()
        self.active = True
        self.last_opportunity_time = 0

    async def run_arbitrage_cycle(self, block_data=None) -> bool:
        return await self.run_cycle(block_data)

    async def run_cycle(self, block_data=None) -> bool:
        block_data = block_data or {}
        slot = None
        if isinstance(block_data, dict):
            slot = block_data.get("context", {}).get("slot")
        if slot is not None:
            logger.info("Websocket block tick: slot=%s", slot)

        try:
            opportunity = await self.detect_opportunity(block_data)
            if opportunity:
                return await self.execute_flash_loan(opportunity)
            return False
        except Exception:
            logger.exception("FlashLoanStrategy cycle failed")
            return False

    async def detect_opportunity(self, block_data: dict[str, Any] | None = None) -> dict | None:
        """Main opportunity detection logic - runs on every new block from Helius WS."""
        block_data = block_data or {}
        try:
            current_time = asyncio.get_running_loop().time()

            if current_time - self.last_opportunity_time < settings.live_trade_cooldown_seconds:
                return None

            sol_price_cex = await self.cex_client.get_price("SOL/USDC")
            sol_price_dex = await self.jupiter.get_implied_price("SOL", "USDC")

            if not sol_price_cex or not sol_price_dex:
                return None

            price_diff = (sol_price_dex - sol_price_cex) / sol_price_cex
            gross_bps = price_diff * 10000

            logger.debug(
                "Price check | CEX: $%.4f | DEX: $%.4f | Gross: %.1fbps",
                sol_price_cex,
                sol_price_dex,
                gross_bps,
            )

            if gross_bps < settings.cex_dex_min_gross_spread_bps:
                return None

            opportunity = await self._calculate_net_opportunity(
                cex_price=sol_price_cex,
                dex_price=sol_price_dex,
                gross_bps=gross_bps,
                block_data=block_data,
            )

            if not opportunity or opportunity["net_bps"] < settings.cex_dex_min_net_spread_bps:
                return None

            if settings.enable_ai_cycle_brain:
                confidence = await self._get_ai_confidence(opportunity)
                if confidence < settings.cex_dex_ai_confidence_floor:
                    logger.info("AI rejected opportunity (confidence: %s%%)", confidence)
                    return None

            trade_size_usdc = await self._calculate_optimal_size(opportunity)
            if trade_size_usdc < settings.cex_dex_min_trade_usdc_micro / 1_000_000:
                return None

            opportunity.update({
                "trade_size_usdc": trade_size_usdc,
                "timestamp": current_time,
                "block_height": block_data.get("blockHeight") if isinstance(block_data, dict) else None,
            })

            self.last_opportunity_time = current_time
            logger.info(
                "✅ Strong opportunity detected! Net: %.1fbps | Size: $%.2f",
                opportunity["net_bps"],
                trade_size_usdc,
            )

            return opportunity

        except Exception as exc:
            logger.error("Opportunity detection error: %s", exc, exc_info=True)
            return None

    async def _calculate_net_opportunity(
        self,
        cex_price: float,
        dex_price: float,
        gross_bps: float,
        block_data: dict,
    ) -> dict:
        costs_bps = (
            settings.cex_dex_cex_fee_roundtrip_bps
            + settings.cex_dex_jupiter_leg_fee_buffer_bps
            + settings.cex_dex_execution_slippage_buffer_bps
            + settings.cex_dex_kamino_flash_fee_bps
            + settings.cex_dex_withdrawal_latency_bps
        )

        net_bps = gross_bps - costs_bps
        flash_fee_estimate = 0.0005 * 10000

        return {
            "gross_bps": gross_bps,
            "net_bps": net_bps,
            "flash_fee_bps": flash_fee_estimate,
            "estimated_profit_usdc": (net_bps / 10000) * (settings.cex_dex_max_trade_usdc_micro / 1_000_000),
            "direction": "buy_cex_sell_dex" if dex_price > cex_price else "buy_dex_sell_cex",
        }

    async def _calculate_optimal_size(self, opportunity: dict) -> float:
        max_size = settings.cex_dex_max_trade_usdc_micro / 1_000_000
        depth_util = await self.oracle.get_cex_depth_utilization()
        size = max_size * min(1.0, depth_util * settings.cex_dex_depth_utilization)
        return min(size, max_size)

    async def _get_ai_confidence(self, opportunity: dict) -> int:
        if not settings.openai_api_key:
            return 85
        return 88

    async def execute_flash_loan(self, opportunity: dict):
        try:
            tx = await self.jupiter.build_flash_loan_swap(
                opportunity=opportunity,
                signer_type=settings.signer_type,
            )

            if not tx:
                return False

            success = await self.jito.simulate_and_send(tx)
            if success:
                logger.info("🚀 Flash loan arbitrage executed successfully!")
            return success

        except Exception as exc:
            logger.error("Execution failed: %s", exc)
            return False

    async def start_background_tasks(self):
        while self.active:
            await asyncio.sleep(60)

    async def start_jito_bundle_monitor(self):
        while self.active:
            await asyncio.sleep(30)
