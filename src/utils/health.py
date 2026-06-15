"""
src/utils/health.py
Production Health & Metrics Endpoint (FastAPI)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse, Response

from src.config.settings import Settings, get_settings
from src.monitoring.health_server import get_redis_status
from src.monitoring.metrics import (
    get_rpc_connection_status,
    get_strategy_metrics,
    get_wallet_balance_summary,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Solana Arb Bot Health")

_start_time = time.time()
_cached_settings: Settings | None = None


def _health_settings() -> Settings:
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = get_settings()
    return _cached_settings


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _system_metrics() -> dict[str, Any]:
    try:
        import psutil
    except ImportError:
        return {"available": False}

    disk_path = "C:\\" if __import__("sys").platform == "win32" else "/"
    return {
        "available": True,
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_free_gb": round(psutil.disk_usage(disk_path).free / (1024**3), 2),
    }


@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Basic health check - used by Docker and orchestrators."""
    settings = _health_settings()
    body: dict[str, Any] = {
        "status": "healthy",
        "timestamp": _utc_timestamp(),
        "uptime_seconds": int(time.time() - _start_time),
        "app_env": settings.app_env,
        "test_mode": settings.test_mode,
        "signer_type": settings.signer_type,
    }
    redis_status = get_redis_status()
    if redis_status is not None:
        body["redis"] = "ok" if redis_status else "unavailable"
        if not redis_status:
            body["status"] = "degraded"
    return body


@app.get("/health/detailed", response_model=None)
async def detailed_health():
    """Detailed health with system + strategy metrics."""
    settings = _health_settings()
    try:
        rpc_status = await get_rpc_connection_status()
        wallet_summary = await get_wallet_balance_summary()
        strategy_metrics = get_strategy_metrics()
        redis_status = get_redis_status()

        overall = "healthy"
        if redis_status is False or not rpc_status.get("healthy", False):
            overall = "degraded"

        return {
            "status": overall,
            "timestamp": _utc_timestamp(),
            "uptime_seconds": int(time.time() - _start_time),
            "app_env": settings.app_env,
            "test_mode": settings.test_mode,
            "signer_type": settings.signer_type,
            "rpc": rpc_status,
            "wallet": wallet_summary,
            "strategies": strategy_metrics,
            "redis": None if redis_status is None else ("ok" if redis_status else "unavailable"),
            "system": _system_metrics(),
        }
    except Exception as exc:
        logger.exception("Detailed health check failed")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "degraded", "error": str(exc)},
        )


@app.get("/health/ready", response_model=None)
async def readiness_check():
    """Kubernetes-style readiness probe."""
    settings = _health_settings()
    if settings.test_mode:
        return {"status": "ready", "mode": "test"}

    rpc_ok = await get_rpc_connection_status()
    redis_status = get_redis_status()
    rpc_healthy = bool(rpc_ok.get("healthy", False))
    redis_healthy = redis_status is not False
    ready = rpc_healthy and redis_healthy

    body = {
        "status": "ready" if ready else "not_ready",
        "rpc_healthy": rpc_healthy,
        "redis_healthy": redis_healthy,
    }
    if not ready:
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=body)
    return body


@app.get("/ready", response_model=None)
async def ready_alias():
    """Alias for legacy probes that hit ``/ready``."""
    return await readiness_check()


@app.get("/metrics", response_model=None)
async def prometheus_metrics():
    """Prometheus-compatible metrics endpoint."""
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={"status": "error", "detail": "prometheus_client not installed"},
        )

    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST.split(";")[0].strip(),
    )


async def start_health_server(settings: Settings | None = None) -> None:
    """Start the FastAPI health server until the asyncio task is cancelled."""
    cfg = settings or get_settings()
    if not cfg.enable_bot_health_server:
        logger.info("Bot health server disabled (ENABLE_BOT_HEALTH_SERVER=false)")
        idle = asyncio.Event()
        try:
            await idle.wait()
        except asyncio.CancelledError:
            return

    import uvicorn

    global _cached_settings
    _cached_settings = cfg

    config = uvicorn.Config(
        app=app,
        host=cfg.bot_health_host,
        port=cfg.bot_health_port,
        log_level="info",
        reload=False,
    )
    server = uvicorn.Server(config)
    logger.info(
        "Bot health server listening on http://%s:%s (/health, /health/detailed, /metrics)",
        cfg.bot_health_host,
        cfg.bot_health_port,
    )

    serve_task = asyncio.create_task(server.serve())
    try:
        await serve_task
    except asyncio.CancelledError:
        server.should_exit = True
        await asyncio.gather(serve_task, return_exceptions=True)
        raise
