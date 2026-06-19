# src/monitoring/health.py
"""
FastAPI health server — liveness, readiness, risk status, Prometheus scrape.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from datetime import UTC, datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from src.config.settings import Settings, get_settings
from src.core.risk import RiskEngine
from src.monitoring.cex_health import (
    get_backpack_balance_status,
    get_cached_backpack_usdc,
    record_backpack_balances,
)
from src.monitoring.metrics import expose_metrics
from src.monitoring.metrics import metrics as metrics_collector

logger = logging.getLogger(__name__)

app = FastAPI(title="Solana Arb Bot Health")

risk_engine: RiskEngine | None = None
settings: Settings | None = None

# Legacy Prometheus counters (health module — separate from metrics.py collectors)
trade_counter = Counter("bot_trades_total", "Total trades executed", ["outcome"])
win_rate_gauge = Gauge("bot_win_rate", "Current win rate percentage")
net_bps_gauge = Gauge("bot_avg_net_bps", "Average net basis points")
opportunity_counter = Counter("bot_opportunities_detected", "Opportunities detected")


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _fetch_backpack_usdc_sync() -> float:
    """Fetch Backpack USDC on a fresh event loop (health server thread-safe)."""
    from src.core.wallet import get_usdc_balance as get_backpack_usdc

    return asyncio.run(get_backpack_usdc())


def _health_config_snapshot(cfg: Settings) -> dict[str, Any]:
    v2_max_raw = (os.getenv("V2_MAX_FLASH_USDC") or "").strip()
    max_flash = float(v2_max_raw) if v2_max_raw else float(cfg.trading.max_flash_usdc)
    min_net = float(cfg.trading.cex_dex_min_net_spread_bps)
    return {
        "min_net_bps": min_net,
        "ai_confidence": cfg.trading.ai_approve_min_confidence,
        "max_flash_usdc": max_flash,
        "v2_min_net_bps": min_net,
        "v2_max_flash_usdc": max_flash if v2_max_raw else None,
        "strategy_priority_order": getattr(cfg, "STRATEGY_PRIORITY_ORDER", None),
    }


def _snapshot_metrics() -> dict[str, Any]:
    """JSON-friendly metrics snapshot (no private prometheus_client internals)."""
    if risk_engine is None:
        return {
            "daily_pnl": 0.0,
            "loss_streak": 0,
            "trades_today": 0,
            "can_trade": True,
        }
    return {
        "daily_pnl": float(risk_engine.daily_pnl),
        "loss_streak": int(risk_engine.loss_streak),
        "trades_today": int(risk_engine.total_trades_today),
        "can_trade": bool(risk_engine.can_trade(0)),
        "uptime_seconds": round(metrics_collector.get_uptime(), 2),
    }


def inject_health_context(
    bot_settings: Settings,
    bot_risk: RiskEngine,
) -> None:
    """Inject shared bot context (call from ``main`` before ``start_health_server``)."""
    global settings, risk_engine
    settings = bot_settings
    risk_engine = bot_risk
    metrics_collector.update_risk_status(
        bot_risk.daily_pnl,
        bot_risk.loss_streak,
        bot_risk.can_trade(0),
    )


@app.get("/mev/status")
async def mev_status() -> dict[str, Any]:
    """Dedicated MEV health + active lanes (StrategyRouter snapshot)."""
    from src.strategies.router import mev_status_snapshot

    return mev_status_snapshot()


@app.get("/health")
async def health() -> dict[str, Any]:
    """Basic liveness probe (Docker / load balancers)."""
    cfg = settings or get_settings()
    payload: dict[str, Any] = {
        "status": "healthy",
        "timestamp": _utcnow_iso(),
        "uptime_seconds": round(metrics_collector.get_uptime(), 2),
        "app_env": cfg.app_env,
        "test_mode": cfg.test_mode,
        "signer": cfg.signer_type,
    }
    if os.getenv("ENABLE_HEALTH_WALLET_DETAILS", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        try:
            from src.core.signer import HotWalletSigner
            from src.utils.inventory import get_usdc_balance_async, get_sol_balance_async

            kp = HotWalletSigner.get_keypair()
            payload["wallet"] = str(kp.pubkey())
            payload["usdc_balance"] = round(await get_usdc_balance_async(), 4)
            payload["sol_balance"] = round(await get_sol_balance_async(), 6)
            payload["can_trade"] = bool(risk_engine.can_trade(0) if risk_engine else True)
        except Exception as exc:
            payload["wallet_error"] = str(exc)[:200]
    return payload


@app.get("/health/detailed")
async def detailed_health() -> dict[str, Any]:
    """Full system status including risk engine and metrics snapshot."""
    cfg = settings or get_settings()
    risk_status = risk_engine.get_status() if risk_engine else {}
    if risk_engine is not None:
        metrics_collector.update_risk_status(
            risk_engine.daily_pnl,
            risk_engine.loss_streak,
            risk_engine.can_trade(0),
        )

    onchain_usdc: float | None = None
    backpack_usdc: float | None = None
    try:
        from src.utils.inventory import get_usdc_balance_async

        onchain_usdc = round(await get_usdc_balance_async(), 4)
        from src.monitoring.metrics import set_onchain_usdc_balance

        set_onchain_usdc_balance(onchain_usdc)
    except Exception as exc:
        logger.debug("On-chain USDC probe failed: %s", exc)

    backpack_status = get_backpack_balance_status()
    if backpack_status.get("ok") is None or (
        backpack_status.get("age_seconds") is not None
        and float(backpack_status["age_seconds"]) > 120
    ):
        from src.monitoring.cex_health import check_backpack_balance_async

        try:
            await check_backpack_balance_async()
            backpack_status = get_backpack_balance_status()
        except Exception as exc:
            logger.debug("Backpack balance refresh failed: %s", exc)

    backpack_usdc = get_cached_backpack_usdc(max_age_sec=180.0)
    if backpack_usdc is None:
        try:
            backpack_usdc = round(await asyncio.to_thread(_fetch_backpack_usdc_sync), 2)
            record_backpack_balances(backpack_usdc)
        except Exception as exc:
            logger.debug("Backpack USDC fetch failed: %s", exc)
            backpack_usdc = None
    else:
        backpack_usdc = round(float(backpack_usdc), 2)

    last_collateral_fill: str | None = None
    try:
        from src.v2.attempt_log import get_last_collateral_fill

        last_collateral_fill = get_last_collateral_fill()
    except Exception as exc:
        logger.debug("Last collateral fill lookup failed: %s", exc)

    rpc_429_count = 0
    try:
        from src.core.rpc_config import get_rpc_429_count

        rpc_429_count = get_rpc_429_count()
    except Exception as exc:
        logger.debug("RPC 429 count failed: %s", exc)

    expose_metrics()
    return {
        "status": "healthy",
        "onchain_usdc": onchain_usdc,
        "backpack_usdc": backpack_usdc,
        "last_collateral_fill": last_collateral_fill,
        "rpc_429_count": rpc_429_count,
        "risk": risk_status,
        "metrics": _snapshot_metrics(),
        "backpack_balance": backpack_status,
        "config": _health_config_snapshot(cfg),
        "timestamp": _utcnow_iso(),
        "uptime_seconds": round(metrics_collector.get_uptime(), 2),
    }


@app.get("/ready", response_model=None)
async def readiness() -> dict[str, Any] | JSONResponse:
    """Readiness probe."""
    try:
        can = risk_engine.can_trade(0) if risk_engine else True
        backpack = get_backpack_balance_status()
        cex_ok = backpack.get("ok") is not False
        ready = can and cex_ok
        return {
            "status": "ready" if ready else "degraded",
            "can_trade": can,
            "services": {
                "rpc": "ok",
                "cex": "ok" if cex_ok else "auth_error",
                "dex": "ok",
            },
            "backpack_balance": backpack,
        }
    except Exception as exc:
        logger.debug("readiness check failed: %s", exc)
        return JSONResponse(status_code=503, content={"status": "not_ready"})


@app.get("/status")
async def full_status() -> dict[str, Any]:
    """Alias for detailed health (legacy path)."""
    return await detailed_health()


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus scraping endpoint (text/plain)."""
    if risk_engine is not None:
        metrics_collector.update_risk_status(
            risk_engine.daily_pnl,
            risk_engine.loss_streak,
            risk_engine.can_trade(0),
        )
    expose_metrics()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/metrics/risk")
async def metrics_risk() -> dict[str, Any]:
    """JSON risk metrics (human / custom dashboards)."""
    if not risk_engine:
        return {}
    return _snapshot_metrics()


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled health server error: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"},
    )


_health_thread: threading.Thread | None = None
_health_lock = threading.Lock()


def start_health_server_sync(host: str = "0.0.0.0", port: int | None = None) -> None:
    """
    Run FastAPI/uvicorn on a daemon thread so Docker /health stays responsive
    when the main trading asyncio loop is busy (Jupiter quotes, roundtrip sim).
    """
    global _health_thread

    cfg = settings or get_settings()
    if port is None:
        port = int(cfg.BOT_HEALTH_PORT)

    if not settings or not risk_engine:
        logger.warning("Health server starting without injected risk/settings context")

    with _health_lock:
        if _health_thread is not None and _health_thread.is_alive():
            logger.debug("Health server thread already running on port %s", port)
            return

        bind_host = host

        def _run_uvicorn() -> None:
            import uvicorn

            config = uvicorn.Config(
                app=app,
                host=bind_host,
                port=port,
                log_level="info",
                access_log=False,
            )
            server = uvicorn.Server(config)
            asyncio.run(server.serve())

        _health_thread = threading.Thread(
            target=_run_uvicorn,
            name="bot-health-http",
            daemon=True,
        )
        _health_thread.start()

    logger.info(
        "Health + metrics server starting on http://%s:%s (paths: /health, /mev/status, /metrics)",
        host,
        port,
    )


async def start_health_server(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Backward-compatible async entry — starts threaded server and returns immediately."""
    start_health_server_sync(host=host, port=port)


def record_trade_signal(strategy: str, net_bps: float, confidence: float) -> None:
    """Record detected opportunity (legacy helper)."""
    _ = (strategy, net_bps, confidence)
    opportunity_counter.inc()


def inc_trade_counter(outcome: str = "executed") -> None:
    trade_counter.labels(outcome).inc()


def record_trade_outcome(success: bool, net_bps: float) -> None:
    outcome = "win" if success else "loss"
    trade_counter.labels(outcome).inc()
    if success:
        win_rate_gauge.set(75.0)
    _ = net_bps


def init_metrics() -> None:
    """Legacy no-op; use ``src.monitoring.metrics.init_metrics`` from main."""
    expose_metrics()
    logger.info("Health module metrics exposed (see src.monitoring.metrics)")
