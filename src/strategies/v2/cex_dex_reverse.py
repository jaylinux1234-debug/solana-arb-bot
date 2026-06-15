# src/strategies/v2/cex_dex_reverse.py
"""
CEX-DEX reverse strategy facade (SOL/USDC) — aggressive fill tuning.

Production execution delegates to ``V2Cycle`` / ``V2ReverseLane`` (Backpack + Jupiter).
This module provides a simplified API compatible with standalone cycle runners.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from solders.keypair import Keypair

from src.config.settings import bootstrap_config, get_settings
from src.core.rpc_manager import RpcManager, get_rpc_manager
from src.core.signer import HotWalletSigner
from src.monitoring.metrics import record_attempt, record_fill
from src.utils.sim import roundtrip_simulator

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


async def _build_v2_cycle():
    """Construct production ``V2Cycle`` (shared with ``src.v2.main``)."""
    from src.cex.backpack import BackpackClient
    from src.core.risk import RiskEngine
    from src.dex.jupiter import JupiterClient
    from src.strategies.dex_cex_reverse import DexCexReverseStrategy as DexCexReverseEngine
    from src.v2.config import V2Config
    from src.v2.cycle import V2Cycle
    from src.v2.dex_cex_reverse import V2ReverseLane

    bootstrap_config()
    settings = get_settings()
    cfg = V2Config.from_env()
    cfg.apply_reverse_env()

    backpack = BackpackClient(settings)
    jupiter = JupiterClient(settings)
    wallet = (
        settings.wallet_pubkey
        or settings.WALLET_PUBKEY
        or os.getenv("WALLET_PUBKEY", "")
    )
    reverse = DexCexReverseEngine(
        jupiter_executor=jupiter,
        backpack_client=backpack,
        wallet_pubkey=wallet,
        settings=settings,
        risk=RiskEngine(settings),
    )
    lane = V2ReverseLane(reverse, cfg)
    return V2Cycle(reverse, cfg, lane)


class CexDexReverseStrategy:
    """
    SOL/USDC dex-cheap reverse — scan, sim, AI heuristic, execute.

    ``run_cycle()`` runs one production v2 cycle (detect → gate → execute).
    """

    def __init__(
        self,
        signer: Keypair | None = None,
        rpc: RpcManager | None = None,
    ) -> None:
        self.signer = signer or HotWalletSigner.get_keypair()
        self.rpc = rpc or get_rpc_manager()
        self._cycle_engine = None
        self._last_trade_ts = 0.0
        self._lock = asyncio.Lock()

    async def _engine(self):
        if self._cycle_engine is None:
            await self.rpc.initialize()
            self._cycle_engine = await _build_v2_cycle()
        return self._cycle_engine

    async def run_cycle(self) -> dict[str, Any] | None:
        """Main cycle — delegates to ``V2Cycle.run_one_cycle``."""
        cooldown = _env_float("LIVE_TRADE_COOLDOWN_SECONDS", 35.0)
        now = time.monotonic()
        if now - self._last_trade_ts < cooldown:
            return None

        async with self._lock:
            record_attempt("cex_dex_reverse")
            engine = await self._engine()
            summary = await engine.run_one_cycle()

        if summary.get("live_fill"):
            record_fill("cex_dex_reverse", summary=summary)
            self._last_trade_ts = time.monotonic()
            logger.info(
                "LIVE FILL | cycle=%s usdc≈%.4f tx=%s",
                summary.get("cycle"),
                float(summary.get("realized_usdc") or 0),
                summary.get("tx_sig", ""),
            )
        return summary

    async def _scan_opportunity(self) -> dict[str, Any] | None:
        """Fast spread scan via production ``V2ReverseLane``."""
        engine = await self._engine()
        lane = engine.lane
        opp = await lane.detect_dex_cheap_signal()
        if not opp:
            probe = lane.last_cycle_probe()
            if probe.get("block_reason"):
                logger.debug("Scan idle: %s", probe.get("block_reason"))
            return None
        return opp

    @staticmethod
    def _calculate_gross_bps(cex_bid: float, jup_price: float) -> float:
        if cex_bid <= 0 or jup_price <= 0:
            return 0.0
        return (float(jup_price) / float(cex_bid) - 1.0) * 10_000.0

    async def _ai_approve(self, opp: dict[str, Any]) -> bool:
        """Heuristic gate — strong gross bypass; optional brain when enabled."""
        gross = float(opp.get("gross_bps") or 0)
        if gross >= _env_float("CEX_DEX_MIN_GROSS_SPREAD_BPS", 6.0):
            return True
        if not os.getenv("ENABLE_AI_CYCLE_BRAIN", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return gross >= _env_float("V2_MIN_NET_BPS", 1.2)

        try:
            from src.brain.ml_brain import ai_approve_opportunity

            return await ai_approve_opportunity(opp)
        except Exception as exc:
            logger.debug("ML brain gate skipped: %s", exc)
            return gross >= _env_float("V2_MIN_NET_BPS", 1.2)

    async def run_cycle_verbose(self) -> dict[str, Any] | None:
        """
        Explicit scan → sim → AI → execute path (for tests / debugging).

        Normal production use should call ``run_cycle()`` only.
        """
        opp = await self._scan_opportunity()
        if not opp:
            return None

        sim = await roundtrip_simulator(opp)
        if not sim.get("passed"):
            logger.info("Sim blocked: %s", sim.get("reason"))
            return {"status": "sim_blocked", **sim}

        if not await self._ai_approve(opp):
            logger.info("AI/heuristic blocked | gross=%.2f", opp.get("gross_bps"))
            return {"status": "ai_blocked", "opportunity": opp}

        return await self.run_cycle()
