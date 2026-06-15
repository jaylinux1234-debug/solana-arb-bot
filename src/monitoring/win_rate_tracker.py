#!/usr/bin/env python3
"""
Independent win-rate tracker — gates live trades on proven setup buckets.

Each trade is keyed by pair + gross/net bps bands. Live execution is allowed only
when that bucket has enough history and win rate above configured floors.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.monitoring.metrics import set_strategy_win_rate
from src.monitoring.telegram_alerts import schedule_telegram

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = os.getenv("WIN_RATE_STATE_PATH", "logs/win_rate_window.json")
# Global floor for ``should_approve()`` (main loop + live execute paths).
LIVE_MIN_WIN_RATE = float(os.getenv("WIN_RATE_MIN_GLOBAL_WIN_RATE", "0.58"))
DEFAULT_WINDOW_HOURS = int(os.getenv("WIN_RATE_WINDOW_HOURS", "48"))


def setup_key(pair: str, gross_bps: float, net_bps: float) -> str:
    """Bucket key for pair + spread shape (used for proven-setup gating)."""
    g = float(gross_bps)
    n = float(net_bps)
    if g < 8:
        gb = "g0_8"
    elif g < 12:
        gb = "g8_12"
    elif g < 16:
        gb = "g12_16"
    else:
        gb = "g16p"
    if n < 3:
        nb = "n0_3"
    elif n < 6:
        nb = "n3_6"
    else:
        nb = "n6p"
    slug = (pair or "SOL/USDC").strip().upper().replace("/", "_")
    return f"{slug}:{gb}:{nb}"


class WinRateTracker:
    def __init__(
        self,
        window_hours: int | None = None,
        path: str | None = None,
    ) -> None:
        self.path = Path(path or DEFAULT_STATE_PATH)
        self.window_hours = int(
            window_hours
            if window_hours is not None
            else os.getenv("WIN_RATE_WINDOW_HOURS", "72")
        )
        self.min_setup_trades = max(
            1, int(os.getenv("WIN_RATE_MIN_SETUP_TRADES", "5"))
        )
        self.min_setup_win_rate = float(
            os.getenv("WIN_RATE_MIN_SETUP_WIN_RATE", "0.65")
        )
        self.min_global_win_rate = LIVE_MIN_WIN_RATE
        self.require_proven_setup = os.getenv(
            "WIN_RATE_REQUIRE_PROVEN_SETUP", "true"
        ).lower() in ("1", "true", "yes", "on")
        self.allow_global_fallback = os.getenv(
            "WIN_RATE_ALLOW_GLOBAL_FALLBACK", "false"
        ).lower() in ("1", "true", "yes", "on")
        self.trades: list[dict[str, Any]] = []
        self._load()

    @staticmethod
    def _bootstrap_warmup_enabled() -> bool:
        return os.getenv("WIN_RATE_BOOTSTRAP_ALLOW", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            rows = data.get("trades", [])
            if isinstance(rows, list):
                normalized = [
                    self._normalize_row(r) for r in rows if isinstance(r, dict)
                ]
                self.trades = [r for r in normalized if r is not None]
            self._prune()
        except Exception as exc:
            logger.debug("WinRateTracker load: %s", exc)
            self.trades = []

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any] | None:
        # Ignore legacy metrics-only rows that don't represent setup-scoped live outcomes.
        # These rows (strategy/success/pnl only) can incorrectly force win-rate=0 and block trading.
        if (
            "trade_id" not in row
            and "setup_key" not in row
            and "gross_bps" not in row
            and "net_bps" not in row
        ):
            return None
        ts = row.get("ts")
        if ts is None:
            ts = time.time()
        pair = str(row.get("pair") or "SOL/USDC")
        gross = float(row.get("gross_bps") or 0.0)
        net = float(row.get("net_bps") or 0.0)
        realized = row.get("realized_usdc")
        if realized is None and row.get("pnl_usd") is not None:
            realized = float(row["pnl_usd"])
        sk = row.get("setup_key") or setup_key(pair, gross, net)
        return {
            "ts": float(ts),
            "trade_id": str(row.get("trade_id") or uuid.uuid4().hex[:12]),
            "gross_bps": gross,
            "net_bps": net,
            "realized_usdc": float(realized or 0.0),
            "success": bool(row.get("success")),
            "pair": pair,
            "setup_key": sk,
        }

    def record_trade(
        self,
        trade_id: str,
        gross_bps: float,
        net_bps: float,
        realized_usdc: float,
        success: bool,
        *,
        pair: str = "SOL/USDC",
    ) -> float:
        entry = {
            "ts": time.time(),
            "trade_id": trade_id,
            "gross_bps": float(gross_bps),
            "net_bps": float(net_bps),
            "realized_usdc": float(realized_usdc),
            "success": bool(success),
            "pair": pair,
            "setup_key": setup_key(pair, gross_bps, net_bps),
        }
        self.trades.append(entry)
        self._prune()
        self._save()

        wr = self.get_win_rate()
        setup = entry["setup_key"]
        setup_wr = self.get_setup_win_rate(setup)
        logger.info(
            "WinRateTracker | trade=%s setup=%s success=%s "
            "setup_wr=%.1f%% global_wr=%.1f%% realized=$%.4f",
            trade_id,
            setup,
            success,
            setup_wr * 100.0,
            wr * 100.0,
            realized_usdc,
        )
        set_strategy_win_rate(pair.replace("/", "_").lower(), wr * 100.0)
        self._maybe_telegram_alert()
        return wr

    def _prune(self) -> None:
        cutoff = time.time() - self.window_hours * 3600
        self.trades = [t for t in self.trades if float(t.get("ts", 0)) > cutoff]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "trades": self.trades,
            "updated": datetime.now(UTC).isoformat(),
            "window_hours": self.window_hours,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get_win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t["success"])
        return wins / len(self.trades)

    def get_setup_win_rate(self, key: str) -> float:
        rows = [t for t in self.trades if t.get("setup_key") == key]
        if not rows:
            return 0.0
        wins = sum(1 for t in rows if t["success"])
        return wins / len(rows)

    def setup_stats(self, key: str) -> dict[str, Any]:
        rows = [t for t in self.trades if t.get("setup_key") == key]
        if not rows:
            return {"setup_key": key, "count": 0, "win_rate": 0.0, "wins": 0}
        wins = sum(1 for t in rows if t["success"])
        return {
            "setup_key": key,
            "count": len(rows),
            "wins": wins,
            "losses": len(rows) - wins,
            "win_rate": wins / len(rows),
        }

    def should_approve(self, min_win_rate: float | None = None) -> bool:
        """Global rolling win rate gate (used by ``main`` + ``execute_trade``)."""
        floor = (
            self.min_global_win_rate
            if min_win_rate is None
            else float(min_win_rate)
        )
        if not self.trades:
            return (not self.require_proven_setup) or self._bootstrap_warmup_enabled()
        if self._bootstrap_warmup_enabled() and len(self.trades) < self.min_setup_trades:
            return True
        return self.get_win_rate() >= floor

    def should_approve_setup(
        self,
        gross_bps: float,
        net_bps: float,
        pair: str = "SOL/USDC",
        *,
        min_win_rate: float | None = None,
        min_trades: int | None = None,
    ) -> tuple[bool, str]:
        """
        Live-trade gate: only approve when this spread bucket is proven.

        Returns ``(ok, reason)``.
        """
        if not self.require_proven_setup:
            return True, "proven_setup_disabled"

        key = setup_key(pair, gross_bps, net_bps)
        floor = self.min_setup_win_rate if min_win_rate is None else float(min_win_rate)
        need = self.min_setup_trades if min_trades is None else int(min_trades)
        stats = self.setup_stats(key)

        if stats["count"] >= need and stats["win_rate"] >= floor:
            return True, f"setup_proven:{key}:{stats['win_rate']:.0%}"

        if self._bootstrap_warmup_enabled() and len(self.trades) < need:
            return True, f"bootstrap_warmup:{len(self.trades)}/{need}"

        if self.allow_global_fallback and len(self.trades) >= need:
            if self.get_win_rate() >= self.min_global_win_rate:
                return True, f"global_fallback:{self.get_win_rate():.0%}"

        if stats["count"] < need:
            return (
                False,
                f"setup_cold:{key} need>={need} have={stats['count']}",
            )
        return (
            False,
            f"setup_low_wr:{key} {stats['win_rate']:.0%}<{floor:.0%}",
        )

    def _maybe_telegram_alert(self) -> None:
        if len(self.trades) < self.min_setup_trades:
            return
        wr = self.get_win_rate()
        if wr * 100.0 >= self.min_global_win_rate * 100.0:
            return
        if os.getenv("TELEGRAM_WIN_RATE_ALERT", "true").lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return
        msg = (
            f"Win rate alert\n"
            f"Global {wr * 100:.1f}% over {len(self.trades)} trades "
            f"({self.window_hours}h window)"
        )
        schedule_telegram(msg, dedupe_key="win_rate_low", cooldown_sec=3600.0)

    def summary(self, strategy: str | None = None) -> dict[str, Any]:
        rows = list(self.trades)
        if strategy:
            needle = strategy.lower().replace("_", "/")
            rows = [
                t
                for t in rows
                if needle in str(t.get("pair", "")).lower()
                or str(t.get("pair", "")).lower().replace("/", "_")
                == strategy.lower()
            ]
        if not rows:
            return {
                "count": 0,
                "win_rate_pct": 0.0,
                "wins": 0,
                "losses": 0,
                "window_hours": self.window_hours,
            }
        wins = sum(1 for t in rows if t["success"])
        return {
            "count": len(rows),
            "wins": wins,
            "losses": len(rows) - wins,
            "win_rate_pct": (wins / len(rows)) * 100.0,
            "window_hours": self.window_hours,
        }

    def record(
        self,
        strategy: str,
        *,
        success: bool,
        pnl_usd: float = 0.0,
        gross_bps: float = 0.0,
        net_bps: float = 0.0,
        trade_id: str | None = None,
        pair: str | None = None,
        slippage_bps: float = 0.0,
    ) -> dict[str, Any]:
        _ = slippage_bps
        pair_label = pair or (
            "SOL/USDC"
            if (strategy or "cex_dex") == "cex_dex"
            else str(strategy).replace("_", "/").upper()
        )
        tid = trade_id or f"{strategy}-{uuid.uuid4().hex[:10]}"
        self.record_trade(
            tid,
            gross_bps=gross_bps,
            net_bps=net_bps,
            realized_usdc=pnl_usd,
            success=success,
            pair=pair_label,
        )
        return self.summary(strategy)


_tracker: WinRateTracker | None = None

# Shared instance (bound from ``main`` after ``bootstrap_config()``).
win_rate_tracker: WinRateTracker | None = None


def bind_win_rate_tracker(tracker: WinRateTracker) -> None:
    """Attach the process-wide tracker (called from ``src.main``)."""
    global win_rate_tracker, _tracker
    win_rate_tracker = tracker
    _tracker = tracker


def get_win_rate_tracker() -> WinRateTracker:
    global _tracker
    if win_rate_tracker is not None:
        return win_rate_tracker
    if _tracker is None:
        _tracker = WinRateTracker()
    return _tracker


def record_trade_outcome(
    strategy: str,
    *,
    success: bool,
    pnl_usd: float = 0.0,
    slippage_bps: float = 0.0,
    gross_bps: float = 0.0,
    net_bps: float = 0.0,
    trade_id: str | None = None,
) -> dict[str, Any]:
    return get_win_rate_tracker().record(
        strategy,
        success=success,
        pnl_usd=pnl_usd,
        slippage_bps=slippage_bps,
        gross_bps=gross_bps,
        net_bps=net_bps,
        trade_id=trade_id,
    )
