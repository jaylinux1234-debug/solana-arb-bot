# src/monitoring/metrics.py
"""Prometheus metrics — core collectors + legacy helpers for health/strategies."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_rpc_ws_status: dict[str, bool] = {}
_prometheus_server_started = False

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

    # ==================== CORE METRICS ====================
    trade_signals_total = Counter(
        "trade_signals_total",
        "Total number of trading signals detected",
        ["strategy", "outcome"],
    )
    trade_executions_total = Counter(
        "trade_executions_total",
        "Total executed trades",
        ["strategy", "success"],
    )
    trade_pnl_usd = Counter(
        "trade_pnl_usd_total",
        "Cumulative PnL in USD",
        ["strategy"],
    )

    daily_pnl_gauge = Gauge("daily_pnl_usd", "Current daily PnL")
    loss_streak_gauge = Gauge("loss_streak", "Current loss streak")
    can_trade_gauge = Gauge("can_trade", "Whether the bot is allowed to trade (1=yes, 0=no)")

    rpc_provider_status = Gauge(
        "rpc_provider_status",
        "RPC provider connection status (1=healthy)",
        ["provider"],
    )
    rpc_failures_total = Counter(
        "rpc_failures_total",
        "Total RPC failures",
        ["provider"],
    )

    opportunity_gross_bps = Histogram(
        "opportunity_gross_bps",
        "Gross spread of detected opportunities (bps)",
        buckets=[30, 40, 50, 60, 80, 100, 150, 200],
    )
    opportunity_net_bps = Histogram(
        "opportunity_net_bps",
        "Net spread after costs (bps)",
        buckets=[20, 30, 40, 50, 60, 80, 100],
    )
    opportunity_size_usdc = Histogram(
        "opportunity_size_usdc",
        "Opportunity size in USDC",
        buckets=[10000, 30000, 50000, 100000, 250000, 500000],
    )

    cycle_latency = Histogram(
        "cycle_latency_seconds",
        "Main cycle execution time",
        buckets=[0.5, 1, 2, 4, 8, 15],
    )
    trade_execution_latency = Histogram(
        "trade_execution_latency_seconds",
        "Time taken to execute a trade",
        buckets=[1, 3, 5, 10, 20],
    )

    bot_uptime_gauge = Gauge("bot_uptime_seconds", "Bot uptime in seconds")
    memory_usage_bytes = Gauge("memory_usage_bytes", "Memory usage")

    # Legacy gauges/counters (existing dashboards / health_server)
    cex_dex_opportunity_gross_bps = Gauge(
        "cex_dex_opportunity_gross_bps",
        "Last detected CEX-DEX gross spread (bps)",
        ["strategy"],
    )
    cex_dex_opportunity_net_bps = Gauge(
        "cex_dex_opportunity_net_bps",
        "Last detected CEX-DEX net spread after costs (bps)",
        ["strategy"],
    )
    rpc_provider_failures_total = Counter(
        "rpc_provider_failures_total",
        "Total Base RPC WebSocket connect / session failures by provider",
        ["provider"],
    )
    rpc_connection_status = Gauge(
        "rpc_connection_status",
        "Base RPC WebSocket health (1=connected, 0=down)",
        ["provider"],
    )
    cex_dex_near_misses_total = Counter("cex_dex_near_misses_total", "Near misses")
    cex_dex_near_miss_total = Counter(
        "cex_dex_near_miss_total",
        "CEX-DEX near misses by gate reason",
        ["reason"],
    )
    bot_realized_profit_usd = Gauge("bot_realized_profit_usd", "Realized PnL")
    WIN_RATE = Gauge("strategy_win_rate", "Win rate %", ["strategy"])
    PROFIT_TODAY = Gauge("daily_profit_usdc", "Daily profit USDC")
    DRAWDOWN_PCT = Gauge("drawdown_pct", "Current drawdown")
    execution_slippage_bps = Histogram(
        "execution_slippage_bps",
        "Realized or modeled execution slippage (bps)",
        ["strategy"],
        buckets=[5, 10, 20, 30, 40, 50, 75, 100, 150, 200],
    )
    rpc_request_latency_seconds = Histogram(
        "rpc_request_latency_seconds",
        "RPC HTTP request latency",
        ["provider", "method"],
        buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
    )
    inventory_reconcile_ok = Gauge(
        "inventory_reconcile_ok",
        "Last inventory reconcile passed (1=yes)",
    )
    circuit_breaker_tripped = Gauge(
        "circuit_breaker_tripped",
        "Circuit breaker manual trip active (1=tripped)",
    )
    onchain_usdc_balance = Gauge("onchain_usdc_balance", "On-chain USDC balance")
    v2_fill_rate = Gauge("v2_fill_rate", "V2 live fill rate (fills / attempts)")

    _PROMETHEUS_OK = True
except ImportError:
    _PROMETHEUS_OK = False
    trade_signals_total = None  # type: ignore[assignment,misc]
    trade_executions_total = None  # type: ignore[assignment,misc]
    trade_pnl_usd = None  # type: ignore[assignment,misc]
    daily_pnl_gauge = None  # type: ignore[assignment,misc]
    loss_streak_gauge = None  # type: ignore[assignment,misc]
    can_trade_gauge = None  # type: ignore[assignment,misc]
    rpc_provider_status = None  # type: ignore[assignment,misc]
    rpc_failures_total = None  # type: ignore[assignment,misc]
    opportunity_gross_bps = None  # type: ignore[assignment,misc]
    opportunity_net_bps = None  # type: ignore[assignment,misc]
    opportunity_size_usdc = None  # type: ignore[assignment,misc]
    cycle_latency = None  # type: ignore[assignment,misc]
    trade_execution_latency = None  # type: ignore[assignment,misc]
    bot_uptime_gauge = None  # type: ignore[assignment,misc]
    memory_usage_bytes = None  # type: ignore[assignment,misc]
    cex_dex_opportunity_gross_bps = None  # type: ignore[assignment,misc]
    cex_dex_opportunity_net_bps = None  # type: ignore[assignment,misc]
    rpc_provider_failures_total = None  # type: ignore[assignment,misc]
    rpc_connection_status = None  # type: ignore[assignment,misc]
    cex_dex_near_misses_total = None  # type: ignore[assignment,misc]
    cex_dex_near_miss_total = None  # type: ignore[assignment,misc]
    bot_realized_profit_usd = None  # type: ignore[assignment,misc]
    WIN_RATE = None  # type: ignore[assignment,misc]
    PROFIT_TODAY = None  # type: ignore[assignment,misc]
    DRAWDOWN_PCT = None  # type: ignore[assignment,misc]
    execution_slippage_bps = None  # type: ignore[assignment,misc]
    rpc_request_latency_seconds = None  # type: ignore[assignment,misc]
    inventory_reconcile_ok = None  # type: ignore[assignment,misc]
    circuit_breaker_tripped = None  # type: ignore[assignment,misc]
    onchain_usdc_balance = None  # type: ignore[assignment,misc]
    v2_fill_rate = None  # type: ignore[assignment,misc]


class MetricsCollector:
    """Centralized metrics recorder."""

    def __init__(self) -> None:
        self.start_time = time.time()
        self._cumulative_daily_pnl = 0.0
        self._v2_attempts = 0
        self._v2_fills = 0

    def _refresh_v2_fill_rate(self) -> None:
        if v2_fill_rate is None:
            return
        rate = self._v2_fills / self._v2_attempts if self._v2_attempts else 0.0
        v2_fill_rate.set(rate)

    def record_trade_signal(
        self,
        strategy: str,
        net_bps: int,
        size_usdc: int,
        confidence: float,
        *,
        gross_bps: int | None = None,
    ) -> None:
        name = (strategy or "cex_dex").strip().lower() or "cex_dex"
        if trade_signals_total is not None:
            trade_signals_total.labels(strategy=name, outcome="detected").inc()
        gross = int(gross_bps if gross_bps is not None else net_bps + 40)
        if opportunity_gross_bps is not None:
            opportunity_gross_bps.observe(gross)
        if opportunity_net_bps is not None:
            opportunity_net_bps.observe(float(net_bps))
        size_usd = float(size_usdc) / 1_000_000.0 if size_usdc > 1_000_000 else float(size_usdc)
        if opportunity_size_usdc is not None:
            opportunity_size_usdc.observe(size_usd)
        record_trade_opportunity(name, gross, int(net_bps))
        logger.info(
            "Signal recorded: %s net=%s size=%.0f conf=%.1f",
            name,
            net_bps,
            size_usd,
            confidence,
        )

    def record_trade_execution(
        self,
        strategy: str,
        success: bool,
        pnl_usd: float = 0.0,
        latency: float = 0.0,
    ) -> None:
        name = (strategy or "cex_dex").strip().lower() or "cex_dex"
        success_label = str(success).lower()
        if trade_executions_total is not None:
            trade_executions_total.labels(strategy=name, success=success_label).inc()
        if pnl_usd != 0.0 and trade_pnl_usd is not None:
            trade_pnl_usd.labels(strategy=name).inc(max(0.0, pnl_usd) if pnl_usd > 0 else 0.0)
            self._cumulative_daily_pnl += pnl_usd
            if daily_pnl_gauge is not None:
                daily_pnl_gauge.set(self._cumulative_daily_pnl)
        if latency > 0 and trade_execution_latency is not None:
            trade_execution_latency.observe(latency)
        # Win-rate ledger: cex_dex_strategy records via WinRateTracker (setup buckets).
        logger.info("Trade execution: %s success=%s pnl=%.2f", name, success, pnl_usd)

    def update_risk_status(
        self,
        daily_pnl: float,
        loss_streak: int,
        can_trade: bool,
    ) -> None:
        if daily_pnl_gauge is not None:
            daily_pnl_gauge.set(float(daily_pnl))
        if loss_streak_gauge is not None:
            loss_streak_gauge.set(float(loss_streak))
        if can_trade_gauge is not None:
            can_trade_gauge.set(1.0 if can_trade else 0.0)

    def update_rpc_status(self, provider: str, healthy: bool) -> None:
        set_rpc_connection_status(provider, healthy)

    def record_failure(self, provider: str) -> None:
        record_rpc_provider_failure(provider)

    def record_cycle(self, duration: float) -> None:
        if cycle_latency is not None:
            cycle_latency.observe(max(0.0, duration))

    def get_uptime(self) -> float:
        return time.time() - self.start_time


# Global collector (user-facing ``metrics`` instance)
metrics = MetricsCollector()


def start_prometheus_server(port: int = 9091) -> None:
    """Optional standalone Prometheus HTTP server (health app also exposes /metrics)."""
    global _prometheus_server_started
    if not _PROMETHEUS_OK or _prometheus_server_started:
        return
    try:
        start_http_server(port)
        _prometheus_server_started = True
        logger.info("Prometheus metrics server started on http://0.0.0.0:%s/metrics", port)
    except Exception as exc:
        logger.error("Failed to start Prometheus server: %s", exc)


def expose_metrics() -> None:
    """Update system gauges (uptime, optional memory)."""
    if bot_uptime_gauge is not None:
        bot_uptime_gauge.set(metrics.get_uptime())
    try:
        import psutil  # type: ignore[import-untyped]

        if memory_usage_bytes is not None:
            memory_usage_bytes.set(float(psutil.Process().memory_info().rss))
    except ImportError:
        pass


def init_metrics() -> None:
    """Initialize metrics; optional dedicated port via METRICS_PROMETHEUS_PORT."""
    expose_metrics()
    port = int(os.getenv("METRICS_PROMETHEUS_PORT", "0"))
    if port > 0:
        start_prometheus_server(port)
    logger.info("Prometheus metrics initialized")


# ==================== LEGACY / MODULE HELPERS ====================


def record_ai_model_update(meta: dict[str, Any]) -> None:
    logger.info(
        "AI model updated | accuracy=%s precision=%s auc=%s n=%s",
        meta.get("accuracy"),
        meta.get("precision"),
        meta.get("auc"),
        meta.get("n_samples"),
    )


def record_bundle_simulation(outcome: dict[str, Any]) -> None:
    logger.info(
        "Bundle simulation | success=%s confidence=%s cu=%s",
        outcome.get("success"),
        outcome.get("confidence"),
        outcome.get("estimated_cu"),
    )


def record_failed_bundle(error: str) -> None:
    logger.warning("Bundle simulation failed: %s", error)


def record_ml_retrain(metadata: dict[str, Any]) -> None:
    logger.info(
        "ML retrain | regime=%s n=%s global_auc=%s regime_auc=%s",
        metadata.get("regime"),
        metadata.get("n_samples"),
        metadata.get("global_auc"),
        metadata.get("regime_auc"),
    )
    record_ai_model_update(
        {
            "auc": metadata.get("global_auc"),
            "n_samples": metadata.get("n_samples"),
            "regime": metadata.get("regime"),
        }
    )


def _near_miss_reason_label(reason: str | None = None) -> str:
    raw = (reason or "unknown").strip()
    return raw.split(":")[0] if ":" in raw else raw or "unknown"


def record_cex_dex_near_miss(gross_bps: float, *, reason: str | None = None) -> None:
    if float(gross_bps) <= 3:
        return
    label = _near_miss_reason_label(reason)
    if cex_dex_near_misses_total is not None:
        cex_dex_near_misses_total.inc()
    if cex_dex_near_miss_total is not None:
        cex_dex_near_miss_total.labels(reason=label).inc()


def set_realized_profit_usd(amount: float) -> None:
    if bot_realized_profit_usd is not None:
        bot_realized_profit_usd.set(float(amount))


def set_strategy_win_rate(strategy: str, win_rate_pct: float) -> None:
    if WIN_RATE is None:
        return
    name = (strategy or "cex_dex").strip().lower() or "cex_dex"
    WIN_RATE.labels(strategy=name).set(float(win_rate_pct))


def set_daily_profit_usdc(amount: float) -> None:
    if PROFIT_TODAY is not None:
        PROFIT_TODAY.set(float(amount))
    if daily_pnl_gauge is not None:
        daily_pnl_gauge.set(float(amount))


def set_drawdown_pct(pct: float) -> None:
    if DRAWDOWN_PCT is not None:
        DRAWDOWN_PCT.set(max(0.0, float(pct)))


def refresh_realized_profit_gauge() -> float:
    try:
        from src.strategies.brain_pnl import realized_pnl_sum_all

        total = realized_pnl_sum_all()
    except Exception as exc:
        logger.debug("refresh_realized_profit_gauge failed: %s", exc)
        total = 0.0
    set_realized_profit_usd(total)
    return total


def record_trade_opportunity(strategy: str, gross_bps: int, net_bps: int | None) -> None:
    name = (strategy or "cex_dex").strip().lower() or "cex_dex"
    if cex_dex_opportunity_gross_bps is not None:
        cex_dex_opportunity_gross_bps.labels(strategy=name).set(float(gross_bps))
    if cex_dex_opportunity_net_bps is not None and net_bps is not None:
        cex_dex_opportunity_net_bps.labels(strategy=name).set(float(net_bps))


def record_attempt(strategy: str) -> None:
    """Record a strategy cycle attempt."""
    name = (strategy or "cex_dex").strip().lower() or "cex_dex"
    metrics._v2_attempts += 1
    metrics._refresh_v2_fill_rate()
    if trade_signals_total is not None:
        trade_signals_total.labels(strategy=name, outcome="attempt").inc()
    logger.debug("Attempt recorded | strategy=%s", name)


def record_fill(strategy: str, *, summary: dict[str, Any] | None = None) -> None:
    """Record a live fill for a strategy lane."""
    name = (strategy or "cex_dex").strip().lower() or "cex_dex"
    metrics._v2_fills += 1
    metrics._refresh_v2_fill_rate()
    profit = 0.0
    if summary:
        profit = float(summary.get("realized_usdc") or summary.get("profit_usdc") or 0)
    record_trade_execution(name, success=True, pnl_usd=profit)


def set_onchain_usdc_balance(amount: float) -> None:
    if onchain_usdc_balance is not None:
        onchain_usdc_balance.set(max(0.0, float(amount)))


def record_trade_signal(
    strategy: str,
    gross_or_net: float,
    net_or_size: float = 0.0,
    confidence: float = 0.0,
    *,
    gross_bps: float | None = None,
) -> None:
    if confidence > 0:
        size_usdc = int(net_or_size)
        gross = int(gross_bps) if gross_bps is not None else None
        metrics.record_trade_signal(
            strategy,
            int(gross_or_net),
            size_usdc,
            confidence,
            gross_bps=gross,
        )
    else:
        logger.info("Signal recorded: %s gross=%.2f net=%.2f", strategy, gross_or_net, net_or_size)
        record_trade_opportunity(strategy, int(gross_or_net), int(net_or_size))


def record_execution_slippage(strategy: str, slippage_bps: float) -> None:
    name = (strategy or "cex_dex").strip().lower() or "cex_dex"
    metrics._last_slippage_bps = float(slippage_bps)
    if execution_slippage_bps is not None:
        execution_slippage_bps.labels(strategy=name).observe(max(0.0, float(slippage_bps)))


def record_rpc_latency(provider: str, method: str, seconds: float) -> None:
    if rpc_request_latency_seconds is None:
        return
    rpc_request_latency_seconds.labels(
        provider=(provider or "unknown").strip().lower() or "unknown",
        method=(method or "rpc").strip().lower() or "rpc",
    ).observe(max(0.0, float(seconds)))


def set_inventory_reconcile_ok(ok: bool) -> None:
    if inventory_reconcile_ok is not None:
        inventory_reconcile_ok.set(1.0 if ok else 0.0)


def set_circuit_breaker_tripped(tripped: bool) -> None:
    if circuit_breaker_tripped is not None:
        circuit_breaker_tripped.set(1.0 if tripped else 0.0)


def record_trade_execution(
    strategy: str,
    *,
    success: bool,
    pnl_usd: float = 0.0,
    latency: float = 0.0,
    slippage_bps: float | None = None,
) -> None:
    if slippage_bps is not None:
        record_execution_slippage(strategy, slippage_bps)
    metrics.record_trade_execution(strategy, success, pnl_usd=pnl_usd, latency=latency)


def record_near_miss(gross_bps: float) -> None:
    record_cex_dex_near_miss(gross_bps)


def record_rpc_provider_failure(provider: str) -> None:
    name = (provider or "unknown").strip().lower() or "unknown"
    if rpc_provider_failures_total is not None:
        rpc_provider_failures_total.labels(provider=name).inc()
    if rpc_failures_total is not None:
        rpc_failures_total.labels(provider=name).inc()


def set_rpc_connection_status(provider: str, up: bool) -> None:
    name = (provider or "unknown").strip().lower() or "unknown"
    _rpc_ws_status[name] = up
    if rpc_connection_status is not None:
        rpc_connection_status.labels(provider=name).set(1.0 if up else 0.0)
    if rpc_provider_status is not None:
        rpc_provider_status.labels(provider=name).set(1.0 if up else 0.0)


def init_rpc_connection_status_gauges(providers: list[str]) -> None:
    for name in providers:
        set_rpc_connection_status(name, False)


async def get_rpc_connection_status() -> dict[str, Any]:
    from src.config.settings import get_settings

    cfg = get_settings()
    rpc_url = (cfg.solana_rpc_url or os.getenv("SOLANA_RPC_URL", "")).strip()
    provider = (cfg.rpc_provider or "unknown").strip().lower() or "unknown"

    if not rpc_url:
        return {"healthy": False, "provider": provider, "message": "rpc_url_not_configured"}

    try:
        from solana.rpc.async_api import AsyncClient

        async with AsyncClient(rpc_url) as client:
            connected = await client.is_connected()
    except Exception as exc:
        logger.debug("RPC health probe failed: %s", exc)
        return {"healthy": False, "provider": provider, "error": str(exc)}

    ws_up = _rpc_ws_status.get(provider)
    return {
        "healthy": connected,
        "provider": provider,
        "http_connected": connected,
        "ws_connected": ws_up,
    }


async def get_wallet_balance_summary() -> dict[str, Any]:
    from src.config.settings import get_settings
    from src.core import wallet as wallet_module

    cfg = get_settings()
    safety = wallet_module.wallet_safety().safety_status()
    summary: dict[str, Any] = {
        "equity_usd": safety.get("equity_usd"),
        "drawdown_pct": safety.get("drawdown_pct"),
        "global_safety_ok": safety.get("global_ok"),
        "successful_simulations": wallet_module.simulation_count(),
        "wallet_pubkey": cfg.wallet_pubkey or None,
    }

    pubkey = (cfg.wallet_pubkey or "").strip()
    rpc_url = (cfg.solana_rpc_url or os.getenv("SOLANA_RPC_URL", "")).strip()
    if not pubkey or not rpc_url:
        return summary

    try:
        from solana.rpc.async_api import AsyncClient
        from solders.pubkey import Pubkey

        async with AsyncClient(rpc_url) as client:
            resp = await client.get_balance(Pubkey.from_string(pubkey))
        summary["balance_sol"] = round(resp.value / 1_000_000_000.0, 6)
    except Exception as exc:
        logger.debug("Wallet balance probe failed: %s", exc)
        summary["balance_error"] = str(exc)

    return summary


def refresh_daily_profit_gauge() -> float:
    try:
        from src.strategies.brain_pnl import rolling_pnl_sum_usd

        total = rolling_pnl_sum_usd(window_seconds=86400.0)
    except Exception as exc:
        logger.debug("refresh_daily_profit_gauge failed: %s", exc)
        total = 0.0
    set_daily_profit_usdc(total)
    return total


def refresh_drawdown_gauge() -> float:
    try:
        from src.core.wallet import wallet_safety

        dd = wallet_safety().drawdown_pct()
    except Exception as exc:
        logger.debug("refresh_drawdown_gauge failed: %s", exc)
        dd = 0.0
    set_drawdown_pct(dd)
    return dd


def get_strategy_metrics() -> dict[str, Any]:
    from src.config.settings import get_settings
    from src.core.circuit_breaker import circuit_breaker

    cfg = get_settings()
    realized_pnl_usd = refresh_realized_profit_gauge()
    profit_today = refresh_daily_profit_gauge()
    drawdown = refresh_drawdown_gauge()
    set_circuit_breaker_tripped(circuit_breaker.is_tripped)
    win_stats = {}
    try:
        from src.monitoring.win_rate_tracker import get_win_rate_tracker

        win_stats = get_win_rate_tracker().summary("cex_dex")
    except Exception:
        pass
    return {
        "circuit_breaker": circuit_breaker.status(),
        "strategy_priority_order": getattr(cfg, "STRATEGY_PRIORITY_ORDER", None),
        "enable_ai_cycle_brain": getattr(cfg, "ENABLE_AI_CYCLE_BRAIN", True),
        "realized_pnl_usd": realized_pnl_usd,
        "daily_profit_usdc": profit_today,
        "drawdown_pct": drawdown,
        "win_rate_cex_dex": win_stats,
    }
