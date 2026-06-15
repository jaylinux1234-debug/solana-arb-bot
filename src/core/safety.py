"""Startup safety checks before the strategy loop runs."""

from __future__ import annotations

import asyncio
import logging

from src.config.settings import Settings
from src.core.security import (
    log_startup_security_advisories,
    require_live_trading_acknowledgement,
    validate_bot_environment,
    validate_rpc_url,
    warn_if_unauthenticated_public_mainnet_rpc,
)
from src.core.wallet import load_safety_state

logger = logging.getLogger(__name__)


async def safety_check(settings: Settings) -> None:
    """Load wallet safety state and fail closed on env / RPC / live-trading policy."""
    await asyncio.to_thread(load_safety_state)

    require_live_trading_acknowledgement(settings.test_mode)

    rpc = (settings.solana_rpc_url or "").strip()
    if rpc:
        validate_rpc_url(rpc)
        warn_if_unauthenticated_public_mainnet_rpc(rpc, logger=logger)

    signing_key = (settings.active_private_key or "").strip()
    if signing_key:
        await asyncio.to_thread(
            validate_bot_environment,
            rpc_url=rpc or settings.solana_rpc_url,
            private_key=signing_key,
            test_mode=settings.test_mode,
        )

    await asyncio.to_thread(log_startup_security_advisories, logger)

    if settings.test_mode:
        logger.info("Running in safe mode (TEST_MODE=true)")
        logger.warning("TEST_MODE=True — no real trades will be executed")
    elif not settings.live_trading_confirm_enabled:
        logger.warning("LIVE_TRADING_CONFIRM is not YES — live sends are blocked by policy")
    else:
        logger.info("Running in LIVE mode (TEST_MODE=false)")

    if settings.ai_approve_min_confidence < 80:
        logger.warning(
            "AI_APPROVE_MIN_CONFIDENCE=%s is below recommended 80 — more trades may pass AI gate",
            settings.ai_approve_min_confidence,
        )
