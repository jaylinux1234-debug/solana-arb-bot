#!/usr/bin/env python3
# src/main.py
"""
Production Solana CEX-DEX Arbitrage Bot

Full integration: RiskEngine + CexDexStrategy + Health Server + Prometheus Metrics + Helius Webhook
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from src.config.settings import Settings, bootstrap_config
from src.core.risk import RiskEngine
from src.core.wallet import initialize_wallet
from src.monitoring.health import inject_health_context, start_health_server_sync
from src.monitoring.metrics import expose_metrics, init_metrics, metrics, start_prometheus_server
from src.monitoring.win_rate_tracker import (
    LIVE_MIN_WIN_RATE,
    WinRateTracker,
    bind_win_rate_tracker,
)
from src.cex.backpack import BackpackClient
from src.dex.jupiter import JupiterClient
from src.strategies.cex_dex_strategy import CexDexStrategy
from src.events.bus import BotEvent, EventKind, get_event_bus
from src.events.webhook.helius import start_helius_webhook, stop_helius_webhook

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    """Read boolean env at runtime (shell overrides beat cached Settings / .env)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _setup_helius_backrun_listener(bot_settings: Settings, bot_strategy: CexDexStrategy) -> None:
    """Register webhook backrun executor when enabled."""
    if not _env_bool("ENABLE_HELIUS_WEBHOOK_BACKRUN", bot_settings.ENABLE_HELIUS_WEBHOOK_BACKRUN):
        return
    from src.dex.jupiter import JupiterClient
    from src.execution.arbitrage import ArbitrageDetector
    from src.execution.helius import HeliusWebhookListener, register_helius_webhook_listener

    listener = HeliusWebhookListener(
        bot_strategy.jito,
        JupiterClient(bot_settings),
        ArbitrageDetector(),
    )
    register_helius_webhook_listener(listener)
    logger.info("Helius webhook backrun listener registered")


# Global components
strategy: CexDexStrategy | None = None
risk_engine: RiskEngine | None = None
settings: Settings | None = None
win_rate_tracker: WinRateTracker | None = None
shutdown_event = asyncio.Event()


async def graceful_shutdown() -> None:
    """Clean shutdown of all components."""
    if shutdown_event.is_set():
        return
    logger.info("Shutdown signal received. Cleaning up...")
    shutdown_event.set()

    try:
        from src.core.singleton import release_singleton

        release_singleton()
    except Exception as exc:
        logger.debug("Singleton release: %s", exc)

    try:
        await stop_helius_webhook()
    except Exception as exc:
        logger.debug("Helius webhook stop: %s", exc)

    if strategy is not None:
        try:
            await strategy.close()
        except Exception as exc:
            logger.debug("Strategy close: %s", exc)

    logger.info("All services stopped. Bot shutdown complete.")


def setup_logging() -> None:
    """Production logging (structlog when USE_STRUCTLOG=true, else plain stdlib)."""
    from src.monitoring.logger import setup_logging as _configure

    _configure(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("jito").setLevel(logging.INFO)


def _register_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """SIGTERM/SIGINT → graceful shutdown (Unix only; use KeyboardInterrupt on Windows)."""
    if sys.platform == "win32":
        return

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: loop.create_task(graceful_shutdown()))
        except (NotImplementedError, RuntimeError) as exc:
            logger.debug("Signal handler not registered for %s: %s", sig, exc)


async def phoenix_startup_self_test() -> None:
    """Verify phoenix-trade import (Docker / local parity)."""
    if not _env_bool("ENABLE_PHOENIX_V1", False):
        logger.info("Phoenix self-test skipped (ENABLE_PHOENIX_V1=false)")
        return
    try:
        from src.dex.phoenix_solana_compat import ensure_phoenix_import_compat

        ensure_phoenix_import_compat()
        from phoenix.market import Market

        logger.info("Phoenix self-test OK | phoenix.market.%s ready", Market.__name__)
    except ImportError as exc:
        logger.warning(
            "Phoenix self-test FAILED — install phoenix-trade in image or disable ENABLE_PHOENIX_V1: %s",
            exc,
        )
    except Exception as exc:
        logger.warning("Phoenix self-test error: %s", exc)


async def main_loop() -> None:
    """Main trading orchestration loop."""
    global strategy, risk_engine, settings, win_rate_tracker

    settings = bootstrap_config()

    from src.core.singleton import ensure_singleton

    await ensure_singleton(settings)

    init_metrics()
    await initialize_wallet()
    await phoenix_startup_self_test()

    # Clear stale inventory_cross_drift trip when chain-held SOL is expected (Ledger workflow).
    if os.getenv("INVENTORY_ALLOW_CHAIN_SOL_HOLDING", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        from src.core.circuit_breaker import circuit_breaker

        if circuit_breaker.trip_reason == "inventory_cross_drift":
            circuit_breaker.reset(force=True)
            logger.info("Cleared stale inventory_cross_drift circuit breaker (chain SOL holding)")

    win_rate_tracker = WinRateTracker()
    bind_win_rate_tracker(win_rate_tracker)
    logger.info(
        "WinRateTracker | window=%sh min_global_wr=%.0f%%",
        win_rate_tracker.window_hours,
        LIVE_MIN_WIN_RATE * 100.0,
    )

    risk_engine = RiskEngine(settings)
    backpack_client = BackpackClient(settings)
    jupiter_executor = JupiterClient(settings)
    wallet_pubkey = (
        settings.wallet_pubkey
        or settings.WALLET_PUBKEY
        or os.getenv("WALLET_PUBKEY", "")
    )
    strategy = CexDexStrategy(
        settings,
        risk_engine=risk_engine,
        win_rate_tracker=win_rate_tracker,
        backpack_client=backpack_client,
        jupiter_executor=jupiter_executor,
        wallet_pubkey=wallet_pubkey,
    )
    logger.info(
        "Strategy wired | vol_gate=VolatilityGate reverse_lane=DexCexReverseStrategy wallet=%s…",
        (wallet_pubkey or "")[:12],
    )

    # Inject context into webhook
    from src.webhook.helius import set_webhook_context

    set_webhook_context(settings, risk_engine, strategy)
    _setup_helius_backrun_listener(settings, strategy)

    inject_health_context(settings, risk_engine)

    logger.info("Starting Solana CEX-DEX Arbitrage Bot")
    logger.info(
        "Environment: %s | Test Mode: %s | Simulate: %s",
        settings.app_env,
        settings.test_mode,
        settings.simulate,
    )
    logger.info(
        "Min Net Spread: %sbps | AI Confidence: %s%% | RPC: %s...",
        settings.trading.cex_dex_min_net_spread_bps,
        settings.trading.ai_approve_min_confidence,
        (settings.solana_rpc_url or "")[:40],
    )

    # === Start background services ===
    if _env_bool("ENABLE_BOT_HEALTH_SERVER", settings.ENABLE_BOT_HEALTH_SERVER):
        health_port = int(os.getenv("BOT_HEALTH_PORT", str(settings.BOT_HEALTH_PORT)))
        start_health_server_sync(
            host=os.getenv("BOT_HEALTH_HOST", settings.BOT_HEALTH_HOST),
            port=health_port,
        )
    else:
        logger.info("Health server disabled (ENABLE_BOT_HEALTH_SERVER=false)")

    prometheus_task: asyncio.Task[None] | None = None
    prom_port = int(os.getenv("METRICS_PROMETHEUS_PORT", "0"))
    if prom_port > 0:
        prometheus_task = asyncio.create_task(
            asyncio.to_thread(start_prometheus_server, prom_port)
        )

    webhook_task: asyncio.Task[None] | None = None
    if settings.ENABLE_HELIUS_WEBHOOK:
        # Keeps running until shutdown (starts aiohttp/FastAPI listener + optional auto-create).
        webhook_task = asyncio.create_task(
            start_helius_webhook(host=os.getenv("HELIUS_WEBHOOK_HOST", "0.0.0.0"))
        )
        logger.info("Helius webhook enabled")
    else:
        logger.info("Helius webhook disabled")

    async def metrics_updater() -> None:
        while not shutdown_event.is_set():
            if risk_engine is not None:
                metrics.update_risk_status(
                    risk_engine.daily_pnl,
                    risk_engine.loss_streak,
                    risk_engine.can_trade(0),
                )
                expose_metrics()
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=15.0)
            except TimeoutError:
                pass

    metrics_task = asyncio.create_task(metrics_updater())

    safety_task: asyncio.Task[None] | None = None
    if _env_bool("ENABLE_GLOBAL_SAFETY_MONITOR", True):
        from src.core.wallet import global_safety_monitor_loop

        safety_interval = float(os.getenv("GLOBAL_SAFETY_INTERVAL_SEC", "60"))
        safety_task = asyncio.create_task(global_safety_monitor_loop(safety_interval))

    backpack_health_task: asyncio.Task[None] | None = None
    if _env_bool("ENABLE_BACKPACK_BALANCE_MONITOR", True):
        from src.monitoring.cex_health import (
            backpack_balance_monitor_loop,
            check_backpack_balance_async,
        )

        backpack_health_task = asyncio.create_task(backpack_balance_monitor_loop())
        await check_backpack_balance_async()
        logger.info("Backpack balance monitor started")

    inventory_task: asyncio.Task[None] | None = None
    if _env_bool(
        "ENABLE_DAILY_INVENTORY_RECONCILE",
        getattr(settings, "ENABLE_DAILY_INVENTORY_RECONCILE", True),
    ):
        try:
            from solders.pubkey import Pubkey
            from solana.rpc.async_api import AsyncClient

            from src.cex.inventory_reconcile import daily_inventory_reconciliation_loop
            from src.core.rpc_urls import resolve_rpc_url

            wallet_pk = (
                settings.wallet_pubkey
                or settings.WALLET_PUBKEY
                or os.getenv("WALLET_PUBKEY", "")
            )
            rpc = resolve_rpc_url("balance")
            if wallet_pk and rpc:

                async def _inventory_loop() -> None:
                    async with AsyncClient(rpc) as client:
                        await daily_inventory_reconciliation_loop(
                            client,
                            Pubkey.from_string(wallet_pk),
                        )

                inventory_task = asyncio.create_task(_inventory_loop())
                logger.info("Inventory reconcile background loop started")
        except Exception as exc:
            logger.warning("Inventory reconcile loop not started: %s", exc)

    loop = asyncio.get_running_loop()
    cycle_count = 0
    try:
        while not shutdown_event.is_set():
            cycle_count += 1
            start_time = loop.time()

            if risk_engine is not None and not risk_engine.can_trade(0):
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=12.0)
                except TimeoutError:
                    pass
                continue

            if (
                settings is not None
                and not settings.test_mode
                and not settings.simulate
                and win_rate_tracker is not None
                and not win_rate_tracker.should_approve(min_win_rate=LIVE_MIN_WIN_RATE)
            ):
                logger.info("Win rate below threshold - skipping")
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=12.0)
                except TimeoutError:
                    pass
                continue

            bus = get_event_bus()
            bus.publish_fire_and_forget(
                BotEvent(kind=EventKind.CYCLE_START, data={"cycle": cycle_count})
            )
            from src.strategies.multi_strategy_cycle import (
                get_last_cycle_context,
                run_unified_cycle,
            )

            success = await run_unified_cycle(strategy, settings)
            ctx = get_last_cycle_context()
            logger.info(
                "Cycle #%s | lane=%s | gross=%.1f | net=%.1f | success=%s",
                cycle_count,
                ctx.get("lane", "?"),
                float(ctx.get("gross_bps") or 0.0),
                float(ctx.get("net_bps") or 0.0),
                success,
            )
            bus.publish_fire_and_forget(
                BotEvent(
                    kind=EventKind.CYCLE_END,
                    data={"cycle": cycle_count, "success": success, **ctx},
                )
            )

            metrics.record_cycle(loop.time() - start_time)

            if cycle_count % 8 == 0 and risk_engine is not None:
                status = risk_engine.get_status()
                logger.info(
                    "Cycle #%s summary | PnL Today: $%.2f | Loss Streak: %s | Can Trade: %s",
                    cycle_count,
                    float(status.get("daily_pnl", 0)),
                    status.get("loss_streak", 0),
                    status.get("can_trade", False),
                )

            from src.strategies.volatility_gate import get_5m_volatility_pct, oracle_poll_sleep_sec

            vol_5m = get_5m_volatility_pct()
            poll_min = float(
                os.getenv(
                    "CEX_DEX_ORACLE_POLL_MIN_SEC",
                    str(getattr(settings, "CEX_DEX_ORACLE_POLL_MIN_SEC", 1.0)),
                )
            )
            poll_max = float(
                os.getenv(
                    "CEX_DEX_ORACLE_POLL_MAX_SEC",
                    str(getattr(settings, "CEX_DEX_ORACLE_POLL_MAX_SEC", 5.0)),
                )
            )
            sleep_seconds = oracle_poll_sleep_sec(vol_5m)
            sleep_seconds = max(poll_min, min(poll_max, sleep_seconds))
            if not success:
                sleep_seconds = max(sleep_seconds, poll_max)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=sleep_seconds)
            except TimeoutError:
                pass

    except asyncio.CancelledError:
        logger.info("Main loop cancelled")
        raise
    except Exception as exc:
        logger.critical("Critical error in main loop: %s", exc, exc_info=True)
        raise
    finally:
        for task in (
            prometheus_task,
            metrics_task,
            webhook_task,
            safety_task,
            backpack_health_task,
            inventory_task,
        ):
            # prometheus_task may be None when disabled
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


async def main() -> None:
    """Application entry point."""
    setup_logging()
    _register_signal_handlers(asyncio.get_running_loop())

    try:
        await main_loop()
    except asyncio.CancelledError:
        logger.info("Main loop task cancelled")
    except KeyboardInterrupt:
        logger.info("Bot stopped manually")
    except Exception as exc:
        logger.critical("Failed to start bot: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        await graceful_shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as exc:
        logger.critical("Failed to start: %s", exc, exc_info=True)
        sys.exit(1)
