"""Post-trade on-chain USDC balance check (profit assert gate)."""

from __future__ import annotations

import logging
import os
from typing import Any

from src.config.settings import Settings, get_settings
from src.core.rpc_urls import resolve_rpc_url

logger = logging.getLogger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _enabled(settings: Settings | None = None) -> bool:
    cfg = settings or get_settings()
    raw = os.getenv("ENABLE_ONCHAIN_PROFIT_ASSERT", "")
    if raw.strip():
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(cfg.risk.enable_onchain_profit_assert)


def min_profit_bps(settings: Settings | None = None) -> int:
    cfg = settings or get_settings()
    return int(
        os.getenv(
            "ONCHAIN_PROFIT_ASSERT_BPS",
            str(
                getattr(
                    cfg,
                    "ONCHAIN_PROFIT_ASSERT_BPS",
                    cfg.risk.onchain_profit_assert_bps,
                )
            ),
        )
    )


def _strict_assert(settings: Settings | None = None) -> bool:
    raw = os.getenv("ONCHAIN_PROFIT_ASSERT_STRICT", "").strip()
    if raw:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    cfg = settings or get_settings()
    return bool(getattr(cfg, "ONCHAIN_PROFIT_ASSERT_STRICT", True))


def profit_assert_threshold_bps(
    *,
    expected_net_bps: float,
    settings: Settings | None = None,
) -> float:
    """Floor for realized on-chain net bps (strict = max(min, modeled); lenient halves modeled)."""
    min_bps = float(min_profit_bps(settings))
    modeled = max(0.0, float(expected_net_bps))
    if _strict_assert(settings):
        return max(min_bps, modeled)
    return max(min_bps, modeled * 0.5)


async def fetch_usdc_balance_micro() -> int | None:
    """Wallet USDC token balance in micro-units (uses configured wallet pubkey)."""
    try:
        from src.core.wallet import get_onchain_usdc_balance

        bal = await get_onchain_usdc_balance()
        return int(bal * 1_000_000)
    except Exception as exc:
        logger.warning("onchain USDC balance fetch failed: %s", exc)
        return None


async def assert_roundtrip_profit(
    *,
    usdc_before_micro: int,
    usdc_after_micro: int,
    trade_size_micro: int,
    expected_net_bps: float,
    settings: Settings | None = None,
) -> tuple[bool, dict[str, Any]]:
    """
    Verify on-chain USDC delta meets minimum modeled net edge.

    Returns ``(ok, details)``. When disabled, always ``(True, {})``.
    """
    if not _enabled(settings):
        return True, {"skipped": True, "reason": "disabled"}

    min_bps = min_profit_bps(settings)
    delta = usdc_after_micro - usdc_before_micro
    realized_bps = (delta / max(trade_size_micro, 1)) * 10_000.0
    threshold_bps = profit_assert_threshold_bps(
        expected_net_bps=float(expected_net_bps),
        settings=settings,
    )

    details = {
        "usdc_before_micro": usdc_before_micro,
        "usdc_after_micro": usdc_after_micro,
        "delta_micro": delta,
        "realized_bps": round(realized_bps, 2),
        "threshold_bps": round(threshold_bps, 2),
        "min_assert_bps": min_bps,
        "expected_net_bps": float(expected_net_bps),
        "strict": _strict_assert(settings),
        "trade_size_micro": trade_size_micro,
    }

    if delta < 0:
        logger.warning("ONCHAIN_PROFIT_ASSERT FAIL | loss delta=%s micro %s", delta, details)
        return False, details

    if realized_bps < threshold_bps:
        logger.warning(
            "ONCHAIN_PROFIT_ASSERT FAIL | realized=%.1fbps need>=%.1fbps %s",
            realized_bps,
            threshold_bps,
            details,
        )
        return False, details

    logger.info(
        "ONCHAIN_PROFIT_ASSERT OK | realized=%.1fbps threshold=%.1fbps",
        realized_bps,
        threshold_bps,
    )
    return True, details
