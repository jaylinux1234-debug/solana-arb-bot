"""Minimal v2 configuration (env-driven, adaptive gates)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class V2Config:
    """Gate bases, adaptive vol knobs, loop timing."""

    min_gross_bps_base: float = 7.0
    min_net_bps_base: float = 1.2
    min_gross_bps: float = 7.0
    min_net_bps: float = 1.2
    adaptive_vol_enabled: bool = True
    vol_lookback_min: int = 5
    vol_high_threshold_pct: float = 1.2
    vol_low_threshold_pct: float = 0.6
    base_cost_bps: float = 7.5
    slippage_buffer_bps: float = 2.8
    jito_tip_bps: float = 1.2
    min_usdc_balance: float = 8.0
    min_trade_usdc: float = 6.0
    execution_slippage_bps: int = 35
    enable_kamino_flash: bool = False
    kamino_prefer_flash: bool = False
    kamino_flash_on_low_inventory: bool = True
    kamino_wallet_first: bool = True
    kamino_flash_amount_usdc_micro: int = 20_000_000
    kamino_lending_market: str = "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF"
    kamino_flash_repay_slippage_bps: int = 30
    max_trade_usdc: float = 12.0
    poll_min_sec: float = 1.5
    poll_max_sec: float = 2.8
    probe_usdc_micro: int = 10_000_000
    max_trade_usdc_micro: int = 12_000_000
    health_port: int = 8001
    enable_health: bool = True
    singleton_key: str = "bot:singleton:v2"
    skip_singleton: bool = False
    attempts_log: str = "logs/v2_attempts.jsonl"
    adaptive_thresholds: bool = True
    vol_high_pct: float = 0.8
    adaptive_min_gross_floor: float = 1.5

    def adaptive_thresholds_for_vol(self, vol_pct: float) -> tuple[float, float]:
        """
        v2.4.1: scale gross min with vol; net min stays at base.

        ``min_gross = max(floor, base * vol_pct / 0.9)``
        """
        if not self.adaptive_vol_enabled:
            return self.min_gross_bps_base, self.min_net_bps_base
        vol_ref = max(0.01, float(vol_pct))
        min_gross = max(
            self.adaptive_min_gross_floor,
            self.min_gross_bps_base * (vol_ref / 0.9),
        )
        return min_gross, self.min_net_bps_base

    @classmethod
    def from_env(cls) -> V2Config:
        try:
            from dotenv import load_dotenv

            root = Path(__file__).resolve().parents[2]
            env_file = root / ".env"
            if env_file.is_file():
                load_dotenv(env_file, override=False)
        except ImportError:
            pass

        def _v2_threshold(primary: str, base: str, default: float) -> float:
            """``V2_MIN_*_BPS`` wins when set; otherwise ``*_BASE``."""
            if os.getenv(primary) is not None:
                return _env_float(primary, default)
            if os.getenv(base) is not None:
                return _env_float(base, default)
            return default

        def _v2_net_threshold(default: float = 1.2) -> float:
            if os.getenv("V2_MIN_NET_BPS") is not None:
                return _env_float("V2_MIN_NET_BPS", default)
            if os.getenv("CEX_DEX_MIN_NET_SPREAD_BPS") is not None:
                return _env_float("CEX_DEX_MIN_NET_SPREAD_BPS", default)
            if os.getenv("V2_MIN_NET_BPS_BASE") is not None:
                return _env_float("V2_MIN_NET_BPS_BASE", default)
            return default

        def _v2_gross_threshold(default: float = 7.0) -> float:
            if os.getenv("V2_MIN_GROSS_BPS") is not None:
                return _env_float("V2_MIN_GROSS_BPS", default)
            if os.getenv("CEX_DEX_MIN_GROSS_SPREAD_BPS") is not None:
                return float(_env_int("CEX_DEX_MIN_GROSS_SPREAD_BPS", int(default)))
            if os.getenv("V2_MIN_GROSS_BPS_BASE") is not None:
                return _env_float("V2_MIN_GROSS_BPS_BASE", default)
            return default

        gross_base = _v2_gross_threshold(7.0)
        net_base = _v2_net_threshold(1.2)
        max_usdc = _env_float("V2_MAX_FLASH_USDC", _env_float("MAX_FLASH_USDC", 12.0))
        max_micro = int(max_usdc * 1_000_000)
        probe = _env_int(
            "V2_PROBE_USDC_MICRO",
            _env_int("CEX_DEX_PROBE_USDC_MICRO", 10_000_000),
        )
        poll_interval = _env_float(
            "V2_POLL_INTERVAL_SEC",
            _env_float("V2_POLL_MAX_SEC", 2.8),
        )
        poll_min = _env_float("V2_POLL_MIN_SEC", min(1.5, poll_interval))
        adaptive_vol = _env_bool("V2_ADAPTIVE_VOL_ENABLED", True)
        legacy_adaptive = _env_bool("V2_ADAPTIVE_THRESHOLDS", adaptive_vol)

        return cls(
            min_gross_bps_base=gross_base,
            min_net_bps_base=net_base,
            min_gross_bps=gross_base,
            min_net_bps=net_base,
            adaptive_vol_enabled=adaptive_vol,
            vol_lookback_min=_env_int("V2_VOL_LOOKBACK_MIN", 5),
            vol_high_threshold_pct=_env_float("V2_VOL_HIGH_THRESHOLD_PCT", 1.2),
            vol_low_threshold_pct=_env_float("V2_VOL_LOW_THRESHOLD_PCT", 0.6),
            base_cost_bps=_env_float(
                "V2_BASE_COST_BPS",
                _env_float("V2_COST_BASE_BPS", 7.5),
            ),
            slippage_buffer_bps=_env_float(
                "V2_SLIPPAGE_BUFFER_BPS",
                _env_float("V2_COST_JUPITER_SLIPPAGE_BPS", 2.8),
            ),
            jito_tip_bps=_env_float("V2_COST_JITO_TIP_BPS", 1.2),
            min_usdc_balance=_env_float("V2_MIN_USDC_BALANCE", 8.0),
            min_trade_usdc=_env_float("V2_MIN_TRADE_USDC", 6.0),
            execution_slippage_bps=_env_int("V2_EXECUTION_SLIPPAGE_BPS", 35),
            enable_kamino_flash=_env_bool("ENABLE_KAMINO_FLASH", False),
            kamino_prefer_flash=_env_bool("V2_KAMINO_PREFER_FLASH", False),
            kamino_flash_on_low_inventory=_env_bool(
                "V2_KAMINO_FLASH_ON_LOW_INVENTORY",
                True,
            ),
            kamino_wallet_first=_env_bool("V2_KAMINO_WALLET_FIRST", True),
            kamino_flash_amount_usdc_micro=_env_int(
                "KAMINO_FLASH_AMOUNT_USDC_MICRO",
                max_micro,
            ),
            kamino_lending_market=os.getenv(
                "KAMINO_LENDING_MARKET",
                "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF",
            ).strip()
            or "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF",
            kamino_flash_repay_slippage_bps=_env_int(
                "KAMINO_FLASH_REPAY_SLIPPAGE_BPS",
                30,
            ),
            max_trade_usdc=max_usdc,
            poll_min_sec=poll_min,
            poll_max_sec=poll_interval,
            probe_usdc_micro=probe,
            max_trade_usdc_micro=min(
                max_micro,
                _env_int("V2_MAX_TRADE_USDC_MICRO", max_micro),
            ),
            health_port=_env_int("V2_HEALTH_PORT", 8001),
            enable_health=_env_bool("V2_ENABLE_HEALTH", True),
            singleton_key=os.getenv("V2_SINGLETON_KEY", "bot:singleton:v2").strip()
            or "bot:singleton:v2",
            skip_singleton=_env_bool("V2_SKIP_SINGLETON", False),
            attempts_log=os.getenv("V2_ATTEMPTS_LOG", "logs/v2_attempts.jsonl").strip()
            or "logs/v2_attempts.jsonl",
            adaptive_thresholds=legacy_adaptive,
            vol_high_pct=_env_float("V2_VOL_HIGH_PCT", 0.8),
            adaptive_min_gross_floor=_env_float(
                "V2_ADAPTIVE_MIN_GROSS_FLOOR",
                gross_base,
            ),
        )

    def apply_reverse_env(self) -> None:
        """Align legacy reverse executor env with v2 gates."""
        os.environ.pop("LEDGER_SIGN_URL", None)
        os.environ.setdefault("SIGNER_TYPE", "hot")
        os.environ.setdefault("CEX_MIDCAPS", "SOL")
        os.environ.setdefault("ENABLE_DEX_CEX_REVERSE", "true")
        os.environ.setdefault("DEX_CEX_REVERSE_MIN_GROSS_BPS", str(self.min_gross_bps_base))
        exec_slip = max(50, int(self.execution_slippage_bps))
        os.environ.setdefault("DEX_CEX_REVERSE_SLIPPAGE_BPS", str(exec_slip))
        os.environ.setdefault("JUPITER_SOL_USDC_SLIPPAGE_BPS", str(exec_slip))
        os.environ.setdefault("LIVE_TRADING_CONFIRM", "YES")
        if self.enable_kamino_flash:
            os.environ.setdefault(
                "KAMINO_MARKET_PUBKEY",
                self.kamino_lending_market,
            )
            os.environ.setdefault(
                "KAMINO_LENDING_MARKET_PUBKEY",
                self.kamino_lending_market,
            )
            usdc_reserve = (os.getenv("KAMINO_USDC_RESERVE") or "").strip()
            if usdc_reserve:
                os.environ.setdefault("KAMINO_USDC_RESERVE", usdc_reserve)
                os.environ.setdefault("KAMINO_FLASH_RESERVE_PUBKEY", usdc_reserve)
        os.environ.setdefault(
            "DEX_CEX_REVERSE_SIZE_USDC_MICRO",
            str(self.max_trade_usdc_micro),
        )
        os.environ.setdefault(
            "CEX_DEX_MAX_TRADE_USDC_MICRO",
            str(self.max_trade_usdc_micro),
        )
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if not os.getenv("BACKPACK_API_KEY_FILE"):
            key_file = os.path.join(root, "secrets", "backpack_api_key")
            if os.path.isfile(key_file):
                os.environ.setdefault("BACKPACK_API_KEY_FILE", key_file)
        if not os.getenv("BACKPACK_SECRET_FILE"):
            sec_file = os.path.join(root, "secrets", "backpack_secret")
            if os.path.isfile(sec_file):
                os.environ.setdefault("BACKPACK_SECRET_FILE", sec_file)
