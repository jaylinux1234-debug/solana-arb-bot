"""
Full Strategy Router — CEX-DEX + MEV (Backrun, Collateral, Liquidations)

Wired to the production codebase (``cex_dex_strategy``, ``multi_strategy_cycle``,
``collateral_swap``, ``liquidation``). In v2 hybrid mode, ``dex_cex_reverse`` stays
on ``V2Cycle``; the router executes MEV lanes only unless ``mev_only=False``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from src.config.settings import Settings, bootstrap_config, get_settings
from src.core.risk import RiskEngine
from src.strategies.brain_signals import (
    backrun_signal_present,
    brain_snapshot,
    collateral_signal_present,
    liquidation_signal_present,
    reset_cycle_signals,
)
from src.strategies.cex_dex_strategy import CexDexStrategy
from src.strategies.multi_strategy_cycle import (
    get_last_cycle_context,
    refresh_backrun_brain_snapshot,
    refresh_cex_dex_brain_snapshot,
    refresh_collateral_brain_snapshot,
    refresh_dex_cex_reverse_brain_snapshot,
    refresh_liquidation_brain_snapshot,
)
logger = logging.getLogger(__name__)

_router_instance: StrategyRouter | None = None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _parse_priority_order(settings: Settings) -> list[str]:
    raw = getattr(settings, "STRATEGY_PRIORITY_ORDER", None)
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if str(s).strip()]
    env_raw = (os.getenv("STRATEGY_PRIORITY_ORDER") or "").strip()
    if env_raw:
        return [s.strip() for s in env_raw.split(",") if s.strip()]
    if isinstance(raw, str) and raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    return [
        "cex_dex",
        "dex_cex_reverse",
        "backrun",
        "collateral_swap",
        "liquidation",
    ]


def _priority_bias(settings: Settings) -> float:
    try:
        return float(
            getattr(settings, "STRATEGY_PRIORITY_SCORE_BIAS", None)
            or os.getenv("STRATEGY_PRIORITY_SCORE_BIAS", "14")
        )
    except (TypeError, ValueError):
        return 14.0


def _format_uptime(seconds: float) -> str:
    total = int(max(0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def get_active_router() -> StrategyRouter | None:
    """Return the live router instance (for health / monitoring)."""
    return _router_instance


class _BaseLaneStrategy:
    """Lane adapter with ``score_opportunity`` + ``execute``."""

    name: str = ""

    def __init__(self, router: StrategyRouter) -> None:
        self._router = router

    async def score_opportunity(self) -> float:
        snap = brain_snapshot()
        return self._score_from_snapshot(snap)

    def _score_from_snapshot(self, snapshot: dict) -> float:
        return 0.0

    async def execute(self) -> dict[str, Any]:
        return {"status": "skipped", "lane": self.name, "filled": False}


class CexDexLaneStrategy(_BaseLaneStrategy):
    name = "cex_dex"

    def _score_from_snapshot(self, snapshot: dict) -> float:
        cx = snapshot.get("cex_dex") or {}
        if not isinstance(cx, dict):
            return 0.0
        try:
            net = float(cx.get("net_bps") or cx.get("spread_bps_net") or 0.0)
            gross = float(cx.get("gross_bps") or cx.get("spread_bps_gross") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return net if net > 0 else gross

    async def execute(self) -> dict[str, Any]:
        strategy = await self._router._ensure_strategy()
        filled = await strategy.run_cycle()
        return {"status": "ok" if filled else "idle", "lane": self.name, "filled": bool(filled)}


class DexCexReverseLaneStrategy(_BaseLaneStrategy):
    name = "dex_cex_reverse"

    def _score_from_snapshot(self, snapshot: dict) -> float:
        ctx = snapshot.get("dex_cex_reverse") or {}
        if not isinstance(ctx, dict):
            return 0.0
        try:
            net = float(ctx.get("net_bps") or 0.0)
            gross = float(ctx.get("gross_bps") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return net if net > 0 else gross

    async def execute(self) -> dict[str, Any]:
        strategy = await self._router._ensure_strategy()
        reverse = strategy.reverse_strategy
        result = await reverse.scan_and_execute()
        filled = bool(result.get("live_fill"))
        return {
            "status": str(result.get("status") or "idle"),
            "lane": self.name,
            "filled": filled,
            "result": result,
        }


class BackrunLaneStrategy(_BaseLaneStrategy):
    name = "backrun"

    def _score_from_snapshot(self, snapshot: dict) -> float:
        if not backrun_signal_present(snapshot):
            br = snapshot.get("backrun") or {}
            if isinstance(br, dict) and br.get("enabled"):
                return 1.0
            return 0.0
        br = snapshot.get("backrun") or {}
        try:
            amt = int(br.get("amount_micro") or 0)
        except (TypeError, ValueError):
            amt = 0
        return float(amt) / 1_000_000.0

    async def execute(self) -> dict[str, Any]:
        snap = brain_snapshot()
        if not backrun_signal_present(snap):
            return {"status": "idle", "lane": self.name, "filled": False}
        filled = await self._router._execute_mev_lane(self.name, snap)
        return {
            "status": "ok" if filled else "profit_threshold",
            "lane": self.name,
            "filled": filled,
        }


class CollateralSwapLaneStrategy(_BaseLaneStrategy):
    name = "collateral_swap"

    def _score_from_snapshot(self, snapshot: dict) -> float:
        col = snapshot.get("collateral_best") or {}
        if not isinstance(col, dict):
            return 0.0
        try:
            score = float(col.get("net_bps") or col.get("spread_bps") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if score > 120:
            score += _env_float("BRAIN_SOFT_FORWARD_SCORE_BOOST", 25.0)
        return score

    async def execute(self) -> dict[str, Any]:
        snap = brain_snapshot()
        filled = await self._router._execute_mev_lane(self.name, snap)
        return {"status": "ok" if filled else "idle", "lane": self.name, "filled": bool(filled)}


class LiquidationLaneStrategy(_BaseLaneStrategy):
    name = "liquidation"

    def _score_from_snapshot(self, snapshot: dict) -> float:
        liq = snapshot.get("liquidation_best") or {}
        if not isinstance(liq, dict):
            return 0.0
        try:
            return float(liq.get("profit_usdc") or liq.get("score") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    async def execute(self) -> dict[str, Any]:
        snap = brain_snapshot()
        filled = await self._router._execute_mev_lane(self.name, snap)
        self._router._liquidation_opps = 1 if liquidation_signal_present(snap) else 0
        return {
            "status": "ok" if filled else "monitoring",
            "lane": self.name,
            "filled": filled,
        }


class StrategyRouter:
    """
    AI Brain + priority router across CEX-DEX and MEV lanes.

    ``mev_only=True`` skips ``cex_dex`` / ``dex_cex_reverse`` so ``V2Cycle`` owns reverse.
    Default ``mev_only=False`` (hybrid): brain picks lane; MEV lanes use ``_execute_mev_lane``.
    """

    def __init__(
        self,
        risk_engine: RiskEngine,
        inventory: Any | None = None,
        *,
        settings: Settings | None = None,
        shutdown_event: Any | None = None,
        mev_only: bool | None = None,
    ) -> None:
        global _router_instance

        self.settings = settings or get_settings()
        self.risk = risk_engine
        self.inventory = inventory
        self._shutdown = shutdown_event
        self._mev_only = (
            mev_only
            if mev_only is not None
            else _env_bool("V2_ROUTER_MEV_ONLY", False)
        )
        self._strategy: CexDexStrategy | None = None
        self._cycle_count = 0
        self._started_at = time.monotonic()
        self._last_selected: str | None = None
        self._last_scores: dict[str, float] = {}
        self._last_result: dict[str, Any] = {}
        self._last_backrun_ts: str | None = None
        self._collateral_opps = 0
        self._liquidation_opps = 0

        self.strategies: dict[str, _BaseLaneStrategy] = {
            "cex_dex": CexDexLaneStrategy(self),
            "dex_cex_reverse": DexCexReverseLaneStrategy(self),
            "backrun": BackrunLaneStrategy(self),
            "collateral_swap": CollateralSwapLaneStrategy(self),
            "liquidation": LiquidationLaneStrategy(self),
        }
        _router_instance = self

    async def _ensure_strategy(self) -> CexDexStrategy:
        if self._strategy is not None:
            return self._strategy
        from src.cex.backpack import BackpackClient
        from src.dex.jupiter import JupiterClient

        backpack = BackpackClient(self.settings)
        jupiter = JupiterClient(self.settings)
        wallet = (
            self.settings.wallet_pubkey
            or self.settings.WALLET_PUBKEY
            or os.getenv("WALLET_PUBKEY", "")
        )
        self._strategy = CexDexStrategy(
            self.settings,
            risk_engine=self.risk,
            backpack_client=backpack,
            jupiter_executor=jupiter,
            wallet_pubkey=wallet,
        )
        self._setup_helius_backrun(self._strategy)
        return self._strategy

    def _setup_helius_backrun(self, strategy: CexDexStrategy) -> None:
        if not _env_bool(
            "ENABLE_HELIUS_WEBHOOK_BACKRUN",
            self.settings.ENABLE_HELIUS_WEBHOOK_BACKRUN,
        ):
            return
        try:
            from src.dex.jupiter import JupiterClient
            from src.execution.arbitrage import ArbitrageDetector
            from src.execution.helius import HeliusWebhookListener, register_helius_webhook_listener

            listener = HeliusWebhookListener(
                strategy.jito,
                JupiterClient(self.settings),
                ArbitrageDetector(),
            )
            register_helius_webhook_listener(listener)
            logger.info("MEV router | Helius backrun listener registered")
        except Exception as exc:
            logger.warning("MEV router | backrun listener setup failed: %s", exc)

    async def _refresh_brain_snapshots(self) -> dict[str, Any]:
        reset_cycle_signals()
        strategy = await self._ensure_strategy()
        await refresh_cex_dex_brain_snapshot(strategy)
        await refresh_dex_cex_reverse_brain_snapshot(strategy)
        await refresh_collateral_brain_snapshot(strategy)
        refresh_backrun_brain_snapshot()
        await refresh_liquidation_brain_snapshot()
        snap = brain_snapshot()
        col = snap.get("collateral_best") or {}
        if isinstance(col, dict) and col.get("active"):
            try:
                self._collateral_opps = int(
                    float(col.get("net_bps") or col.get("spread_bps") or 0) > 0
                )
            except (TypeError, ValueError):
                self._collateral_opps = 0
        liq = snap.get("liquidation_best") or {}
        if isinstance(liq, dict) and liquidation_signal_present(snap):
            self._liquidation_opps = 1
        return snap

    def _note_backrun_event(self, snapshot: dict) -> None:
        br = snapshot.get("backrun") or {}
        if isinstance(br, dict) and br.get("active"):
            self._last_backrun_ts = datetime.now(UTC).isoformat()

    def _collateral_min_spread_bps(self) -> float:
        raw = os.getenv("COLLATERAL_MIN_SPREAD_BPS", "").strip()
        if raw:
            return float(raw)
        return float(getattr(self.settings, "COLLATERAL_MIN_SPREAD_BPS", 35))

    def _collateral_min_net_bps(self) -> float:
        raw = os.getenv("COLLATERAL_MIN_NET_BPS", "").strip()
        if raw:
            return float(raw)
        return self._collateral_min_spread_bps()

    async def _build_mev_snapshot(self) -> dict[str, Any]:
        """Refresh collateral/liquidation; preserve backrun via TTL cache."""
        reset_cycle_signals()
        strategy = await self._ensure_strategy()
        await refresh_collateral_brain_snapshot(strategy)
        refresh_backrun_brain_snapshot()
        await refresh_liquidation_brain_snapshot()
        snap = brain_snapshot()
        col = snap.get("collateral_best") or {}
        if isinstance(col, dict) and col.get("active"):
            try:
                self._collateral_opps = int(
                    float(col.get("net_bps") or col.get("spread_bps") or 0) > 0
                )
            except (TypeError, ValueError):
                self._collateral_opps = 0
        if liquidation_signal_present(snap):
            self._liquidation_opps = 1
        return snap

    def _pick_mev_lane(self, snapshot: dict[str, Any]) -> str | None:
        """Binary pick with backrun priority."""
        if backrun_signal_present(snapshot):
            return "backrun"
        col = snapshot.get("collateral_best") or {}
        if isinstance(col, dict):
            try:
                net_bps = float(col.get("net_bps") or col.get("spread_bps") or 0.0)
            except (TypeError, ValueError):
                net_bps = 0.0
            if net_bps > self._collateral_min_net_bps() or collateral_signal_present(snapshot):
                return "collateral_swap"
        if liquidation_signal_present(snapshot):
            return "liquidation"
        return None

    async def _execute_mev_lane(self, lane: str, snapshot: dict[str, Any]) -> bool:
        """Unified MEV dispatcher — delegates to ``mev_dispatch.execute_mev_lane``."""
        from src.strategies.mev_dispatch import execute_mev_lane

        success = await execute_mev_lane(lane, snapshot, settings=self.settings)
        if success and lane == "backrun":
            self._note_backrun_event(snapshot)
        return success

    def _mev_logging_enabled(self) -> bool:
        return _env_bool("ENABLE_MEV_LOGGING", False)

    def _log_mev_attempt(
        self,
        lane: str,
        snapshot: dict[str, Any],
        success: bool,
    ) -> None:
        """Append MEV cycle to v2_attempts.jsonl for mev_watch."""
        from src.v2.attempt_log import append_attempt

        br = snapshot.get("backrun") if isinstance(snapshot.get("backrun"), dict) else {}
        col = snapshot.get("collateral_best") if isinstance(snapshot.get("collateral_best"), dict) else {}
        liq = snapshot.get("liquidation_best") if isinstance(snapshot.get("liquidation_best"), dict) else {}
        lane_ctx = br if lane == "backrun" else col if lane == "collateral_swap" else liq

        if lane == "mev_idle" and not self._mev_logging_enabled():
            return

        record = {
            "cycle": self._cycle_count,
            "lane": lane,
            "event": "MEV_CYCLE",
            "executed": success,
            "live_fill": success and lane != "liquidation",
            "gross_bps": float(lane_ctx.get("gross_bps") or 0.0),
            "net_bps": float(lane_ctx.get("net_bps") or lane_ctx.get("spread_bps") or 0.0),
            "block_reason": lane if success else "mev_idle",
            "backrun_amount_micro": br.get("amount_micro"),
            "collateral_spread_bps": col.get("spread_bps"),
            "liquidation_profit_usdc": liq.get("profit_usdc"),
        }
        append_attempt(
            os.getenv("V2_ATTEMPTS_LOG", "logs/v2_attempts.jsonl"),
            record,
        )
        if self._mev_logging_enabled():
            logger.info(
                "MEV_LOG | lane=%s success=%s backrun_usdc=%.2f collateral_spread=%s liq_profit=%s",
                lane,
                success,
                float(br.get("amount_micro") or 0) / 1_000_000.0,
                col.get("spread_bps"),
                liq.get("profit_usdc"),
            )

    async def run_mev_router_cycle(self) -> bool:
        """Fixed MEV cycle with TTL-safe signals + JSONL logging."""
        import src.strategies.multi_strategy_cycle as msc

        snapshot = await self._build_mev_snapshot()
        picked = self._pick_mev_lane(snapshot)
        if not picked:
            msc._last_cycle_ctx = {"lane": "mev_idle", "gross_bps": 0.0, "net_bps": 0.0}
            self._log_mev_attempt("mev_idle", snapshot, False)
            return False

        msc._last_cycle_ctx = {
            "lane": picked,
            "gross_bps": 0.0,
            "net_bps": 0.0,
            "brain_conf": 0,
        }
        logger.info("MEV router | executing_lane=%s", picked)
        success = await self._execute_mev_lane(picked, snapshot)
        self._log_mev_attempt(picked, snapshot, success)
        return success

    async def select_best_strategy(self) -> str:
        """AI Brain + priority router."""
        await self._refresh_brain_snapshots()

        scores: dict[str, float] = {}
        for name, strat in self.strategies.items():
            scores[name] = await strat.score_opportunity()

        priority = _parse_priority_order(self.settings)
        bias = _priority_bias(self.settings)
        for i, name in enumerate(priority):
            if name in scores:
                scores[name] += (len(priority) - i) * bias

        best = max(scores, key=lambda k: scores[k])
        self._last_scores = scores
        self._last_selected = best
        logger.info("Strategy selected: %s (score=%.1f)", best, scores[best])
        return best

    async def execute(self, strategy_name: str) -> dict[str, Any]:
        if strategy_name not in self.strategies:
            return {"status": "error", "reason": "unknown_strategy", "filled": False}

        if self._mev_only and strategy_name in ("cex_dex", "dex_cex_reverse"):
            return {
                "status": "delegated_v2_cycle",
                "lane": strategy_name,
                "filled": False,
            }

        if not self.risk.can_trade(0):
            return {"status": "risk_blocked", "lane": strategy_name, "filled": False}

        if strategy_name in ("backrun", "collateral_swap", "liquidation"):
            snapshot = await self._build_mev_snapshot()
            filled = await self._execute_mev_lane(strategy_name, snapshot)
            self._log_mev_attempt(strategy_name, snapshot, filled)
            result = {
                "status": "ok" if filled else "idle",
                "lane": strategy_name,
                "filled": bool(filled),
            }
        elif self._mev_only:
            filled = await self.run_mev_router_cycle()
            ctx = get_last_cycle_context()
            result = {
                "status": "ok" if filled else "idle",
                "lane": str(ctx.get("lane") or strategy_name),
                "filled": bool(filled),
                "gross_bps": ctx.get("gross_bps"),
                "net_bps": ctx.get("net_bps"),
            }
        else:
            result = await self.strategies[strategy_name].execute()

        self._last_result = result
        return result

    def mev_status(self) -> dict[str, Any]:
        """Snapshot for ``/mev/status`` health endpoint."""
        uptime = time.monotonic() - self._started_at
        active: list[str] = []
        if _env_bool("ENABLE_HELIUS_WEBHOOK_BACKRUN", False):
            active.append("backrun")
        if _env_bool("ENABLE_COLLATERAL_RATE_ARB", False):
            active.append("collateral_swap")
        if _env_bool("ENABLE_LIQUIDATION_MONITORING", False):
            active.append("liquidation")

        snap = brain_snapshot()
        br = snap.get("backrun") if isinstance(snap.get("backrun"), dict) else {}
        col = snap.get("collateral_best") if isinstance(snap.get("collateral_best"), dict) else {}
        liq = snap.get("liquidation_best") if isinstance(snap.get("liquidation_best"), dict) else {}

        return {
            "status": "healthy",
            "mev_only": self._mev_only,
            "active_lanes": active,
            "enabled": {
                "backrun": _env_bool("ENABLE_HELIUS_WEBHOOK_BACKRUN", False),
                "collateral_swap": _env_bool("ENABLE_COLLATERAL_RATE_ARB", False),
                "liquidation": _env_bool("ENABLE_LIQUIDATION_MONITORING", False),
            },
            "last_selected": self._last_selected,
            "last_scores": self._last_scores,
            "last_result": self._last_result,
            "last_backrun": self._last_backrun_ts or br.get("last_seen"),
            "backrun_active": backrun_signal_present(snap),
            "collateral_opps": self._collateral_opps or int(bool(col.get("active"))),
            "collateral_spread_bps": col.get("spread_bps"),
            "liquidation_opps": self._liquidation_opps or int(liquidation_signal_present(snap)),
            "liquidation_profit_usdc": liq.get("profit_usdc"),
            "router_cycles": self._cycle_count,
            "router_uptime": _format_uptime(uptime),
            "router_uptime_seconds": round(uptime, 1),
            "priority_order": _parse_priority_order(self.settings),
        }

    async def run_forever(self) -> None:
        """Main MEV + CEX loop."""
        bootstrap_config()
        await self._ensure_strategy()
        order = _parse_priority_order(self.settings)
        logger.info(
            "StrategyRouter started | priority=%s mev_only=%s backrun=%s collateral=%s liquidation=%s",
            order,
            self._mev_only,
            _env_bool("ENABLE_HELIUS_WEBHOOK_BACKRUN", False),
            _env_bool("ENABLE_COLLATERAL_RATE_ARB", False),
            _env_bool("ENABLE_LIQUIDATION_MONITORING", False),
        )

        poll_min = float(os.getenv("CEX_DEX_ORACLE_POLL_MIN_SEC", "1.2"))
        poll_max = float(os.getenv("CEX_DEX_ORACLE_POLL_MAX_SEC", "4.0"))

        while self._shutdown is None or not self._shutdown.is_set():
            self._cycle_count += 1
            sleep_sec = poll_max
            try:
                best = await self.select_best_strategy()
                result = await self.execute(best)
                if result.get("filled"):
                    logger.info("FILL: %s | %s", best, result)
                elif result.get("lane") not in (None, "mev_idle", best):
                    logger.debug("Router cycle | %s | %s", best, result.get("status"))

                ctx = get_last_cycle_context()
                if self._mev_only:
                    logger.info(
                        "MEV cycle #%s | lane=%s | gross=%.1f | net=%.1f | success=%s",
                        self._cycle_count,
                        ctx.get("lane", result.get("lane", "?")),
                        float(ctx.get("gross_bps") or 0.0),
                        float(ctx.get("net_bps") or 0.0),
                        bool(result.get("filled")),
                    )
                sleep_sec = poll_max if not result.get("filled") else poll_min
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Router error: %s", exc, exc_info=True)
                sleep_sec = 5.0

            if self._shutdown is not None:
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_sec)
                    break
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(sleep_sec)

        if self._strategy is not None:
            try:
                await self._strategy.close()
            except Exception as exc:
                logger.debug("StrategyRouter close: %s", exc)
        logger.info("StrategyRouter stopped")


def mev_status_snapshot() -> dict[str, Any]:
    """Module-level MEV status for health server (no router instance required)."""
    router = get_active_router()
    if router is not None:
        return router.mev_status()
    return {
        "status": "idle",
        "active_lanes": [],
        "message": "StrategyRouter not running",
    }
