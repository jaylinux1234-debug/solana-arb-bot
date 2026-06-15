# src/core/wallet_safety.py
"""Wallet + inventory safety: sim gate, cooldown, hourly/daily caps, drawdown, volume limits."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.config.settings import get_settings, settings

logger = logging.getLogger(__name__)


class WalletSafety:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.daily_loss = 0.0
        self.trades_today = 0
        self.last_trade_time: datetime | None = None

    async def check_all_safety_gates(self) -> bool:
        """Run all safety checks"""
        if self.daily_loss > self.settings.max_daily_loss_usdc:
            logger.critical(f"DAILY LOSS LIMIT BREACHED: ${self.daily_loss}")
            return False

        if self.trades_today >= self.settings.max_live_trades_per_hour:
            logger.warning("Hourly trade limit reached")
            return False

        return True

    def record_trade(self, pnl_usdc: float) -> None:
        self.daily_loss += max(0, -pnl_usdc)  # Only count losses
        self.trades_today += 1
        self.last_trade_time = datetime.now()

    def should_pause_trading(self) -> bool:
        if self.last_trade_time and (datetime.now() - self.last_trade_time).total_seconds() < self.settings.live_trade_cooldown_seconds:
            return True
        return False


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_day() -> str:
    return _utc_now().strftime("%Y-%m-%d")


def _utc_trade_hour() -> str:
    return _utc_now().strftime("%Y-%m-%d-%H")


@dataclass
class WalletSafetyState:
    successful_sims: int = 0
    daily_volume_usdc: int = 0
    trades_this_hour: int = 0
    last_trade_ts: float = 0.0
    inventory_sol: float = 0.0
    drawdown_pct: float = 0.0
    # Extended / legacy persistence
    live_trades_today: int = 0
    live_trades_day: str = ""
    trade_hour_bucket: str = ""
    daily_volume_usdc_micro: int = 0
    daily_volume_date: str = ""
    successful_sim_count: int = 0
    last_update: str = ""
    equity_high_water_usd: float = 0.0
    last_equity_usd: float = 0.0
    last_global_safety_ts: float = 0.0


class PersistentWalletSafety:
    """Persistent safety state with async trade gating."""

    def __init__(self, state_path: str | None = None) -> None:
        self.state_path = Path(state_path or settings.WALLET_SAFETY_STATE_PATH)
        self.state = self._load()
        self.lock = asyncio.Lock()

    def _load(self) -> WalletSafetyState:
        if not self.state_path.is_file():
            return WalletSafetyState(live_trades_day=_utc_day(), daily_volume_date=_utc_day())

        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return WalletSafetyState()
            return self._state_from_dict(raw)
        except Exception as exc:
            logger.warning("wallet_safety: could not load state (%s)", exc)
            return WalletSafetyState()

    @staticmethod
    def _state_from_dict(data: dict[str, Any]) -> WalletSafetyState:
        sims = int(data.get("successful_sims") or data.get("successful_sim_count") or 0)
        vol_micro = int(data.get("daily_volume_usdc_micro") or data.get("daily_volume_usdc") or 0)
        last_ts = float(data.get("last_trade_ts") or data.get("last_live_trade_ts") or 0.0)
        hour_bucket = str(data.get("trade_hour_bucket") or "")
        trades_hour = int(data.get("trades_this_hour") or 0)
        if hour_bucket and hour_bucket != _utc_trade_hour():
            trades_hour = 0
            hour_bucket = _utc_trade_hour()

        day = _utc_day()
        live_day = str(data.get("live_trades_day") or data.get("daily_volume_date") or day)
        live_today = int(data.get("live_trades_today") or 0)
        if live_day != day:
            live_today = 0
            live_day = day
            vol_micro = 0

        return WalletSafetyState(
            successful_sims=sims,
            successful_sim_count=sims,
            daily_volume_usdc=vol_micro,
            daily_volume_usdc_micro=vol_micro,
            trades_this_hour=trades_hour,
            trade_hour_bucket=hour_bucket or _utc_trade_hour(),
            last_trade_ts=last_ts,
            live_trades_today=live_today,
            live_trades_day=live_day,
            daily_volume_date=str(data.get("daily_volume_date") or live_day),
            inventory_sol=float(data.get("inventory_sol") or 0.0),
            drawdown_pct=float(data.get("drawdown_pct") or 0.0),
            last_update=str(data.get("last_update") or ""),
            equity_high_water_usd=float(data.get("equity_high_water_usd") or 0.0),
            last_equity_usd=float(data.get("last_equity_usd") or 0.0),
            last_global_safety_ts=float(data.get("last_global_safety_ts") or 0.0),
        )

    def _roll_buckets(self) -> None:
        day = _utc_day()
        if self.state.live_trades_day != day:
            self.state.live_trades_today = 0
            self.state.live_trades_day = day
            self.state.daily_volume_date = day
            self.state.daily_volume_usdc = 0
            self.state.daily_volume_usdc_micro = 0

        hour = _utc_trade_hour()
        if self.state.trade_hour_bucket != hour:
            self.state.trades_this_hour = 0
            self.state.trade_hour_bucket = hour

    def _save(self) -> None:
        self.state.last_update = _utc_now().isoformat()
        self.state.successful_sim_count = self.state.successful_sims
        self.state.daily_volume_usdc_micro = self.state.daily_volume_usdc
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(asdict(self.state), indent=2),
            encoding="utf-8",
        )

    def reload(self) -> WalletSafetyState:
        self.state = self._load()
        return self.state

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.state)

    def set_drawdown_pct(self, pct: float) -> None:
        self.state.drawdown_pct = max(0.0, float(pct))
        self._save()

    def set_inventory_sol(self, sol: float) -> None:
        self.state.inventory_sol = max(0.0, float(sol))
        self._save()

    def record_simulation(self) -> None:
        self.state.successful_sims += 1
        self.state.successful_sim_count = self.state.successful_sims
        self._save()
        logger.info("Simulation count: %s", self.state.successful_sims)

    def record_trade(self, size_usdc_micro: int) -> None:
        """Record a completed live trade (``size_usdc_micro`` = USDC lamports, 6 decimals)."""
        if size_usdc_micro <= 0:
            return
        self._roll_buckets()
        self.state.daily_volume_usdc += int(size_usdc_micro)
        self.state.daily_volume_usdc_micro = self.state.daily_volume_usdc
        self.state.trades_this_hour += 1
        self.state.live_trades_today += 1
        self.state.last_trade_ts = time.time()
        self._save()

    def _check_trade_unlocked(self, size_usdc_micro: int) -> tuple[bool, str]:
        from src.core.circuit_breaker import circuit_breaker

        if settings.test_mode:
            return True, "test_mode"

        if circuit_breaker.should_pause():
            return False, "circuit_breaker"

        self._roll_buckets()

        max_dd = float(
            getattr(settings, "MAX_DRAWDOWN_PCT", None)
            or settings.trading.max_drawdown_pct
        )
        if self.state.drawdown_pct > max_dd:
            return False, f"drawdown_{self.state.drawdown_pct:.2f}pct"

        if self.state.inventory_sol > settings.INVENTORY_MAX_SOL:
            return False, f"inventory_{self.state.inventory_sol:.2f}_sol"

        cooldown = max(0, settings.LIVE_TRADE_COOLDOWN_SECONDS)
        if cooldown > 0 and self.state.last_trade_ts > 0:
            elapsed = time.time() - self.state.last_trade_ts
            if elapsed < cooldown:
                return False, "cooldown"

        hourly_cap = max(0, settings.MAX_LIVE_TRADES_PER_HOUR)
        if hourly_cap > 0 and self.state.trades_this_hour >= hourly_cap:
            return False, "hourly_limit"

        daily_cap = max(0, settings.MAX_LIVE_TRADES_PER_DAY)
        if daily_cap > 0 and self.state.live_trades_today >= daily_cap:
            return False, "daily_limit"

        min_sims = max(0, settings.MIN_SUCCESSFUL_SIMS_BEFORE_LIVE)
        if self.state.successful_sims < min_sims:
            if settings.ENFORCE_MIN_SIMS_BEFORE_LIVE:
                return False, f"insufficient_sims_{self.state.successful_sims}_need_{min_sims}"

        vol_cap = max(0, settings.MAX_DAILY_VOLUME_USDC_MICRO)
        if vol_cap > 0 and size_usdc_micro > 0:
            if self.state.daily_volume_usdc + size_usdc_micro > vol_cap:
                return False, "daily_volume_cap"

        max_single = max(0, settings.MAX_SINGLE_TRADE_USDC_MICRO)
        if max_single > 0 and size_usdc_micro > max_single:
            return False, "max_single_trade"

        return True, "ok"

    async def can_trade(self, size_usdc_micro: int) -> tuple[bool, str]:
        async with self.lock:
            return self._check_trade_unlocked(size_usdc_micro)

    def check_global_safety(self) -> bool:
        """Return False when drawdown, trade caps, or circuit breaker blocks the bot loop."""
        from src.core.circuit_breaker import circuit_breaker

        if circuit_breaker.should_pause():
            logger.warning("Circuit breaker active — global safety failed")
            return False

        self._roll_buckets()

        max_dd = float(
            getattr(settings, "MAX_DRAWDOWN_PCT", None)
            or settings.trading.max_drawdown_pct
        )
        if self.state.drawdown_pct > max_dd:
            logger.critical(
                "MAX DRAWDOWN BREACHED (%.2f%% > %.2f%%) — BOT PAUSED",
                self.state.drawdown_pct,
                max_dd,
            )
            return False

        daily_cap = max(0, settings.MAX_LIVE_TRADES_PER_DAY)
        if daily_cap > 0 and self.state.live_trades_today >= daily_cap:
            logger.warning(
                "Daily trade limit reached (%s/%s)",
                self.state.live_trades_today,
                daily_cap,
            )
            return False

        return True

    def before_live_send(self, usdc_amount_micro: int) -> tuple[bool, str]:
        ok, reason = self._check_trade_unlocked(usdc_amount_micro)
        if not ok:
            logger.warning("Wallet safety: blocking live send (%s)", reason)
        return ok, reason


wallet_safety = PersistentWalletSafety()


# --- Module-level API (backward compatible with wallet.py + strategies) ---

SAFETY_STATE_PATH = str(wallet_safety.state_path)


def load_safety_state() -> dict[str, Any]:
    wallet_safety.reload()
    logger.info(
        "Wallet safety state loaded | successful_sims=%s drawdown_pct=%.2f inventory_sol=%.4f",
        wallet_safety.state.successful_sims,
        wallet_safety.state.drawdown_pct,
        wallet_safety.state.inventory_sol,
    )
    return wallet_safety.to_dict()


def save_safety_state() -> None:
    wallet_safety._save()


def get_state() -> dict[str, Any]:
    return wallet_safety.to_dict()


def merge_state(data: dict[str, Any]) -> None:
    """Apply dict fields onto persisted state (used by ``wallet.WalletSafety`` coordinator)."""
    st = wallet_safety.state
    for key, value in data.items():
        if hasattr(st, key):
            setattr(st, key, value)
    wallet_safety._save()


def set_drawdown_pct(pct: float) -> None:
    wallet_safety.set_drawdown_pct(pct)


def set_inventory_sol(sol: float) -> None:
    wallet_safety.set_inventory_sol(sol)


def check_global_safety() -> bool:
    return wallet_safety.check_global_safety()


def record_successful_simulation() -> None:
    wallet_safety.record_simulation()


def record_live_trade_usdc_micro(amount_micro: int) -> None:
    wallet_safety.record_trade(amount_micro)


def simulation_count() -> int:
    return wallet_safety.state.successful_sims


def before_live_send(usdc_amount_micro: int) -> tuple[bool, str]:
    if not check_global_safety():
        return False, "global_safety_failed"
    return wallet_safety.before_live_send(usdc_amount_micro)


def record_cex_reconciliation(delta_sol: float) -> None:
    import os

    from src.core.circuit_breaker import circuit_breaker
    from src.utils.alerts import schedule_alert

    try:
        thresh = float(os.getenv("CEX_RECONCILIATION_CRITICAL_DRIFT_SOL", "2.0"))
    except (TypeError, ValueError):
        thresh = 2.0

    drift = abs(float(delta_sol))
    if drift <= thresh:
        return
    msg = f"LARGE CEX INVENTORY DRIFT: {delta_sol:.4f} SOL (threshold={thresh:.4f})"
    logger.critical(msg)
    schedule_alert(msg)
    if os.getenv("CEX_RECONCILIATION_TRIP_BREAKER", "true").lower() in ("1", "true", "yes"):
        circuit_breaker.trip(f"cex_reconcile_drift_{drift:.3f}sol")


check_safety = check_global_safety
