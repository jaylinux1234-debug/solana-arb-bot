#!/usr/bin/env python3
"""
V2 Main — Hybrid: CEX-DEX Reverse + Full MEV Router (Backrun, Collateral, Liquidations)

Runs optimized V2 reverse cycles in parallel with the MEV strategy router.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from dotenv import load_dotenv

load_dotenv()

from src.config.settings import bootstrap_config, get_settings
from src.core.risk import RiskEngine
from src.core.wallet import initialize_wallet
from src.cex.backpack import BackpackClient
from src.dex.jupiter import JupiterClient
from src.monitoring.metrics import init_metrics
from src.strategies.dex_cex_reverse import DexCexReverseStrategy
from src.strategies.router import StrategyRouter
from src.v2.config import V2Config
from src.v2.cycle import V2Cycle
from src.v2.dex_cex_reverse import V2ReverseLane

logger = logging.getLogger(__name__)

shutdown_event = asyncio.Event()


def setup_logging() -> None:
    from pathlib import Path

    log_path = Path(os.getenv("V2_LOG_FILE", "logs/v2.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger(__name__).info("v2 hybrid logging to %s", log_path.resolve())


def _register_signals(loop: asyncio.AbstractEventLoop) -> None:
    def _handler() -> None:
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _handler())


def _configure_stdio_utf8() -> None:
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


async def _ensure_v2_singleton(cfg: V2Config) -> None:
    if cfg.skip_singleton:
        logger.warning("V2_SKIP_SINGLETON=true — multiple instances allowed")
        return
    os.environ["BOT_SINGLETON_NEXTLEVEL_KEY"] = cfg.singleton_key
    settings = bootstrap_config()
    from src.core.singleton import ensure_singleton

    await ensure_singleton(settings)


async def _maybe_health(cfg: V2Config, risk: RiskEngine) -> None:
    if not cfg.enable_health:
        return
    os.environ.setdefault("BOT_HEALTH_PORT", str(cfg.health_port))
    from src.monitoring.health import inject_health_context, start_health_server_sync

    settings = get_settings()
    inject_health_context(settings, risk)
    start_health_server_sync()
    logger.info("v2 health server on port %s (/health)", cfg.health_port)


async def _build_v2_cycle(risk: RiskEngine, settings) -> V2Cycle:
    cfg = V2Config.from_env()
    cfg.apply_reverse_env()
    from src.core.cost_model import reset_advanced_cost_model
    from src.strategies.backrun_executor import reset_backrun_executor
    from src.strategies.collateral_executor import reset_collateral_executor
    from src.strategies.liquidation_executor import reset_liquidation_executor
    from src.v2.cost_model import refresh_cost_model

    reset_advanced_cost_model()
    reset_backrun_executor()
    reset_collateral_executor()
    reset_liquidation_executor()
    refresh_cost_model(cfg)
    backpack = BackpackClient(settings)
    jupiter = JupiterClient(settings)
    wallet = (
        settings.wallet_pubkey
        or settings.WALLET_PUBKEY
        or os.getenv("WALLET_PUBKEY", "")
    )
    reverse = DexCexReverseStrategy(
        jupiter_executor=jupiter,
        backpack_client=backpack,
        wallet_pubkey=wallet,
        settings=settings,
        risk=risk,
    )
    lane = V2ReverseLane(reverse, cfg)
    return V2Cycle(reverse, cfg, lane, shutdown_event=shutdown_event)


async def run() -> None:
    """Boot hybrid v2 reverse + MEV router."""
    _configure_stdio_utf8()
    bootstrap_config()
    settings = get_settings()
    cfg = V2Config.from_env()

    from src.core.signer import HotWalletSigner

    kp = HotWalletSigner.get_keypair()
    logger.info(
        "Hot wallet ready | pubkey=%s | signer=hot",
        str(kp.pubkey()),
    )

    logger.info("Starting V2 Hybrid with Full MEV Support")
    logger.info("Strategies: %s", getattr(settings, "STRATEGY_PRIORITY_ORDER", "?"))
    logger.info(
        "MEV Enabled: Backrun=%s Collateral=%s Liquidation=%s Webhook=%s",
        _env_bool("ENABLE_HELIUS_WEBHOOK_BACKRUN", False),
        _env_bool("ENABLE_COLLATERAL_RATE_ARB", False),
        _env_bool("ENABLE_LIQUIDATION_MONITORING", False),
        _env_bool("ENABLE_HELIUS_WEBHOOK", getattr(settings, "ENABLE_HELIUS_WEBHOOK", False)),
    )

    if cfg.enable_health:
        os.environ["BOT_HEALTH_PORT"] = str(cfg.health_port)
        os.environ["ENABLE_BOT_HEALTH_SERVER"] = "true"

    from src.core.rpc_config import get_upgraded_robust_provider

    rpc_provider = get_upgraded_robust_provider(force_reload=True)
    await _ensure_v2_singleton(cfg)
    await initialize_wallet()

    risk_engine = RiskEngine(settings)
    init_metrics()
    await _maybe_health(cfg, risk_engine)

    from src.monitoring.cex_health import (
        backpack_balance_monitor_loop,
        check_backpack_balance_async,
    )

    backpack_health_task = asyncio.create_task(
        backpack_balance_monitor_loop(),
        name="backpack_health",
    )
    try:
        await check_backpack_balance_async()
    except Exception as exc:
        logger.warning("Initial Backpack balance check failed: %s", exc)

    v2_cycle = await _build_v2_cycle(risk_engine, settings)
    strategy_router = StrategyRouter(
        risk_engine=risk_engine,
        inventory=v2_cycle.inventory,
        settings=settings,
        shutdown_event=shutdown_event,
        mev_only=_env_bool("V2_ROUTER_MEV_ONLY", False),
    )

    logger.info(
        "v2 hybrid boot | rpc=%s max_usdc=%.0f kamino_flash=%s",
        list(rpc_provider.providers.keys()),
        cfg.max_trade_usdc,
        cfg.enable_kamino_flash,
    )

    webhook_task: asyncio.Task[None] | None = None
    if settings.ENABLE_HELIUS_WEBHOOK and _env_bool(
        "ENABLE_HELIUS_WEBHOOK_BACKRUN",
        getattr(settings, "ENABLE_HELIUS_WEBHOOK_BACKRUN", False),
    ):
        from src.events.webhook.helius import start_helius_webhook

        webhook_host = os.getenv("HELIUS_WEBHOOK_HOST", "0.0.0.0")
        webhook_port = int(os.getenv("HELIUS_WEBHOOK_PORT", "8799"))
        webhook_task = asyncio.create_task(
            start_helius_webhook(host=webhook_host, port=webhook_port),
            name="helius_webhook",
        )
        logger.info(
            "Helius webhook started for v2 backrun | host=%s port=%s",
            webhook_host,
            webhook_port,
        )

    tasks = [
        asyncio.create_task(v2_cycle.run_forever(), name="v2_cycle"),
        asyncio.create_task(strategy_router.run_forever(), name="mev_router"),
        backpack_health_task,
    ]

    from src.strategies.meme_sniping import meme_sniping_settings, detect_new_pools

    if meme_sniping_settings.enabled:
        tasks.append(
            asyncio.create_task(
                detect_new_pools(shutdown_event),
                name="meme_sniping",
            )
        )
        logger.info(
            "Meme Sniping Strategy activated | simulate=%s max_trade_sol=%.2f",
            meme_sniping_settings.simulate,
            meme_sniping_settings.max_trade_sol,
        )

    if webhook_task is not None:
        tasks.append(webhook_task)

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        task_names = ["v2_cycle", "mev_router"] + (
            ["meme_sniping"] if meme_sniping_settings.enabled else []
        ) + (["helius_webhook"] if webhook_task is not None else [])
        for name, result in zip(task_names, results, strict=True):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.error("Task %s failed: %s", name, result, exc_info=result)
    except asyncio.CancelledError:
        logger.info("Shutting down V2 Hybrid...")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if webhook_task is not None:
            try:
                from src.events.webhook.helius import stop_helius_webhook

                await stop_helius_webhook()
            except Exception as exc:
                logger.debug("Helius webhook shutdown: %s", exc)
        from src.core.singleton import release_singleton

        release_singleton()
        logger.info("v2 hybrid shutdown complete")


async def main() -> None:
    setup_logging()
    loop = asyncio.get_running_loop()
    _register_signals(loop)
    await run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("v2 hybrid stopped")
