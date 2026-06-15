#!/usr/bin/env python3
"""Backrun executor — webhook-driven, profitability-gated, Jito bundle."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import src.core.wallet as wallet_safety
from src.config.settings import Settings, get_settings
from src.core.cost_model import (
    AdvancedCostModel,
    BackrunEstimate,
    get_advanced_cost_model,
)
from src.dex.jupiter import JupiterExecutor, get_jupiter_executor
from src.execution.arbitrage import ArbitrageDetector
from src.execution.jito_bundle import (
    JitoBundleExecutor,
    get_jito_bundle_executor,
    resolve_dynamic_jito_tip_lamports,
)
from src.v2.attempt_log import append_attempt

logger = logging.getLogger(__name__)

_executor: BackrunExecutor | None = None
_recent_victim_sigs: dict[str, float] = {}


def _dedup_ttl_sec() -> float:
    return _env_float("BACKRUN_DEDUP_TTL_SEC", 45.0)


def _was_recently_processed(victim_sig: str | None) -> bool:
    if not victim_sig:
        return False
    now = time.monotonic()
    expired = [k for k, ts in _recent_victim_sigs.items() if now - ts > _dedup_ttl_sec()]
    for k in expired:
        _recent_victim_sigs.pop(k, None)
    return victim_sig in _recent_victim_sigs


def _mark_processed(victim_sig: str | None) -> None:
    if victim_sig:
        _recent_victim_sigs[victim_sig] = time.monotonic()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class BackrunExecutor:
    """Profitability-gated Helius/Jupiter 3-leg backrun via Jito bundle."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        jupiter: JupiterExecutor | None = None,
        jito: JitoBundleExecutor | None = None,
        detector: ArbitrageDetector | None = None,
        cost_model: AdvancedCostModel | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.jupiter = jupiter
        self.jito = jito
        self.detector = detector
        self.cost_model = cost_model or get_advanced_cost_model()

    def _min_amount_micro(self) -> int:
        return int(
            os.getenv(
                "HELIUS_BACKRUN_MIN_AMOUNT_MICRO",
                str(getattr(self.settings, "HELIUS_BACKRUN_MIN_AMOUNT_MICRO", 35_000_000)),
            )
        )

    def _min_net_bps(self) -> float:
        return _env_float(
            "BACKRUN_MIN_NET_BPS",
            _env_float("CEX_DEX_MIN_NET_SPREAD_BPS", 1.8),
        )

    def _min_profit_usd(self) -> float:
        return _env_float("BACKRUN_MIN_PROFIT_USD", 3.0)

    def _trade_fraction(self) -> float:
        raw = os.getenv("BACKRUN_SIZE_FRACTION", "").strip()
        if raw:
            return max(0.1, min(1.0, float(raw)))
        return max(0.1, min(1.0, _env_float("HELIUS_BACKRUN_AMOUNT_FRACTION", 0.5)))

    def _quote_slippage_bps(self) -> int:
        return int(
            os.getenv(
                "BACKRUN_QUOTE_SLIPPAGE_BPS",
                os.getenv("BACKRUN_SLIPPAGE_BPS", "70"),
            )
        )

    def _trade_micro(self, amount_micro: int) -> int:
        """Conservative sizing: min(fraction-based, victim_amount // 3)."""
        fraction_micro = max(1, int(amount_micro * self._trade_fraction()))
        if _env_bool("BACKRUN_CONSERVATIVE_SIZE", True):
            conservative = max(1, amount_micro // 3)
            return min(fraction_micro, conservative)
        return fraction_micro

    async def _is_duplicate(self, tx_sig: str | None) -> bool:
        if not tx_sig:
            return False
        if _was_recently_processed(tx_sig):
            return True
        try:
            from src.utils.redis import get_redis

            redis = await get_redis()
            if redis is None:
                return False
            key = f"backrun:dedup:{tx_sig}"
            return bool(await redis.exists(key))
        except Exception as exc:
            logger.debug("Backrun redis dedup check failed: %s", exc)
            return False

    async def _mark_duplicate(self, tx_sig: str | None) -> None:
        if not tx_sig:
            return
        _mark_processed(tx_sig)
        try:
            from src.utils.redis import get_redis

            redis = await get_redis()
            if redis is None:
                return
            ttl = max(1, int(_dedup_ttl_sec()))
            await redis.setex(f"backrun:dedup:{tx_sig}", ttl, "1")
        except Exception as exc:
            logger.debug("Backrun redis dedup mark failed: %s", exc)

    async def _fetch_backrun_quotes(
        self,
        trade_micro: int,
        midcap_mint: str,
    ) -> dict[str, Any]:
        """Try multi-hop quotes first, then direct-route fallback."""
        slippage = self._quote_slippage_bps()
        quotes = await self.detector.get_jupiter_route_quotes_for_backrun(
            trade_micro,
            midcap_mint,
            slippage_bps=slippage,
            only_direct=False,
        )
        if quotes and quotes.get("quote3_sol_to_usdc"):
            return quotes

        logger.debug(
            "Backrun multi-hop quote miss — trying direct routes | mint=%s",
            midcap_mint[:12],
        )
        return await self.detector.get_jupiter_route_quotes_for_backrun(
            trade_micro,
            midcap_mint,
            slippage_bps=min(slippage, 50),
            only_direct=True,
        )

    async def _ensure_clients(self) -> None:
        if self.jupiter is None:
            self.jupiter = get_jupiter_executor()
        if self.jito is None:
            self.jito = get_jito_bundle_executor(self.settings)
        if self.detector is None:
            self.detector = ArbitrageDetector()

    async def execute(self, victim_ctx: dict[str, Any]) -> bool:
        """Improved backrun with Redis/memory dedup + multi-hop quoting."""
        await self._ensure_clients()

        amount_micro = int(victim_ctx.get("amount_micro") or 0)
        victim_sig = str(victim_ctx.get("tx_sig") or victim_ctx.get("signature") or "") or None
        midcap_mint = str(
            victim_ctx.get("midcap_mint")
            or victim_ctx.get("output_mint")
            or victim_ctx.get("input_mint")
            or ""
        )

        if await self._is_duplicate(victim_sig):
            logger.debug("Backrun dedup skip | sig=%s", (victim_sig or "")[:16])
            self._log_attempt(victim_ctx, None, "dedup")
            return False

        if amount_micro < self._min_amount_micro() or not midcap_mint:
            self._log_attempt(victim_ctx, None, "below_min_amount")
            return False

        await self._mark_duplicate(victim_sig)

        if _env_bool("TEST_MODE", self.settings.test_mode):
            logger.info(
                "TEST_MODE — skip live backrun | mint=%s… amt=%s",
                midcap_mint[:8],
                amount_micro,
            )
            self._log_attempt(victim_ctx, None, "test_mode")
            return False

        trade_micro = self._trade_micro(amount_micro)

        try:
            quotes = await self._fetch_backrun_quotes(trade_micro, midcap_mint)
            q3 = quotes.get("quote3_sol_to_usdc")
            if not quotes or not q3 or "outAmount" not in q3:
                logger.debug("Backrun incomplete quotes | mint=%s", midcap_mint[:12])
                self._log_attempt(victim_ctx, None, "quote_failed")
                return False

            modeled = self.cost_model.estimate_backrun(quotes, trade_micro)
            if (
                modeled.net_bps < self._min_net_bps()
                or modeled.profit_usd < self._min_profit_usd()
            ):
                logger.info(
                    "Backrun below threshold | net_bps=%.2f profit=$%.2f min_net=%.2f "
                    "min_profit=$%.2f mint=%s… trade_micro=%s",
                    modeled.net_bps,
                    modeled.profit_usd,
                    self._min_net_bps(),
                    self._min_profit_usd(),
                    midcap_mint[:8],
                    trade_micro,
                )
                self._log_attempt(victim_ctx, modeled, "profit_threshold")
                return False

            ok_w, wreason = wallet_safety.before_live_send(trade_micro)
            if not ok_w:
                logger.warning("Backrun blocked by wallet safety: %s", wreason)
                self._log_attempt(victim_ctx, modeled, f"safety_{wreason}")
                return False

            tip_lamports = self._calculate_dynamic_tip(modeled)
            bundle_id = await self._send_backrun_bundle(quotes, trade_micro, tip_lamports)
            success = bool(bundle_id)
            if success:
                wallet_safety.record_live_trade_usdc_micro(trade_micro)
            self._log_attempt(
                victim_ctx,
                modeled,
                "success" if success else "send_fail",
                extra={"bundle_id": bundle_id, "tip_lamports": tip_lamports},
            )
            return success

        except Exception as exc:
            logger.error("Backrun failed: %s", exc, exc_info=True)
            self._log_attempt(victim_ctx, None, "exception")
            return False

    def _calculate_dynamic_tip(self, modeled: BackrunEstimate) -> int:
        if _env_bool("JITO_DYNAMIC_TIP", True):
            return resolve_dynamic_jito_tip_lamports(
                modeled.net_bps,
                modeled.trade_usdc_micro,
                gross_bps=modeled.gross_bps,
            )
        return int(os.getenv("JITO_TIP_LAMPORTS", str(self.settings.JITO_TIP_LAMPORTS)))

    async def _send_backrun_bundle(
        self,
        quotes: dict[str, Any],
        trade_micro: int,
        tip_lamports: int,
    ) -> str | None:
        wallet = (
            self.settings.wallet_pubkey
            or self.settings.WALLET_PUBKEY
            or os.getenv("WALLET_PUBKEY", "")
        )
        if not wallet:
            logger.error("Backrun bundle: WALLET_PUBKEY not set")
            return None

        slippage = int(
            os.getenv(
                "BACKRUN_SLIPPAGE_BPS",
                os.getenv("MAX_SLIPPAGE_BPS", str(self.settings.MAX_SLIPPAGE_BPS)),
            )
        )
        txs_b64: list[str] = []
        for key in ("quote1_usdc_to_mid", "quote2_mid_to_sol", "quote3_sol_to_usdc"):
            quote = quotes.get(key)
            if not quote or "outAmount" not in quote:
                return None
            swap_data = await self.jupiter.build_swap_transaction(
                {"quote": quote},
                str(wallet),
                slippage_bps=slippage,
            )
            if not swap_data or "swapTransaction" not in swap_data:
                logger.warning("Backrun swap build failed | leg=%s", key)
                return None
            txs_b64.append(swap_data["swapTransaction"])

        result = await self.jito.send_bundle_b64(txs_b64, tip_lamports=tip_lamports)
        if result.get("success"):
            return str(result.get("bundle_id") or result.get("txid") or "")
        logger.warning("Backrun Jito send failed: %s", result.get("error"))
        return None

    def _log_attempt(
        self,
        ctx: dict[str, Any],
        modeled: BackrunEstimate | None,
        status: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "lane": "backrun",
            "event": "BACKRUN_ATTEMPT",
            "executed": status == "success",
            "live_fill": status == "success",
            "block_reason": status,
            "victim_sig": ctx.get("tx_sig") or ctx.get("signature"),
            "midcap_mint": ctx.get("midcap_mint"),
            "amount_micro": ctx.get("amount_micro"),
            "gross_bps": modeled.gross_bps if modeled else 0.0,
            "net_bps": modeled.net_bps if modeled else 0.0,
            "profit_usd": modeled.profit_usd if modeled else 0.0,
            "total_cost_bps": modeled.total_cost_bps if modeled else 0.0,
        }
        if extra:
            record.update(extra)
        append_attempt(
            os.getenv("V2_ATTEMPTS_LOG", "logs/v2_attempts.jsonl"),
            record,
        )
        if _env_bool("ENABLE_MEV_LOGGING", False):
            logger.info(
                "BACKRUN_LOG | status=%s sig=%s net_bps=%.2f profit=$%.2f",
                status,
                record.get("victim_sig"),
                record.get("net_bps", 0),
                record.get("profit_usd", 0),
            )


def get_backrun_executor(settings: Settings | None = None) -> BackrunExecutor:
    global _executor
    if _executor is None:
        _executor = BackrunExecutor(settings=settings)
    return _executor


def reset_backrun_executor() -> BackrunExecutor:
    global _executor
    _recent_victim_sigs.clear()
    _executor = BackrunExecutor()
    return _executor
