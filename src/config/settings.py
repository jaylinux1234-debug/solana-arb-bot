# src/config/settings.py
"""
Pydantic v2 settings — nested trading/risk models + legacy UPPER_CASE env aliases.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Annotated, Any

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class TradingSettings(BaseModel):
    min_net_profit_bps: int = Field(48, ge=1)
    cex_dex_min_gross_spread_bps: int = Field(55, ge=1)
    cex_dex_min_net_spread_bps: float = Field(48.0, ge=0.5)
    ai_approve_min_confidence: float = Field(88.0, ge=50.0, le=99.0)
    cex_dex_ai_confidence_floor: float = Field(84.0, ge=50.0)
    live_trade_cooldown_seconds: int = Field(150, ge=30)
    max_live_trades_per_hour: int = Field(4, ge=0)
    dynamic_amount: bool = True
    min_flash_usdc: float = Field(30_000.0)
    max_flash_usdc: float = Field(500_000.0)
    flash_size_utilization: float = Field(0.68, ge=0.4, le=0.85)
    max_drawdown_pct: float = Field(3.0, ge=1.0)
    max_inventory_sol: float = Field(45.0)
    volatility_filter_bps: int = Field(120)
    cex_dex_depth_utilization: float = Field(0.65)
    cex_dex_strategy_base_cost_bps: int = Field(62, ge=8)
    cex_dex_log_near_misses: bool = False


class RiskSettings(BaseModel):
    max_daily_loss_usdc: float = Field(500.0)
    circuit_breaker_loss_streak: int = Field(3)
    circuit_breaker_loss_usd: float = Field(80.0)
    enable_onchain_profit_assert: bool = True
    onchain_profit_assert_bps: int = Field(20, ge=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "compose.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_nested_delimiter="__",
    )

    # Core (new schema)
    app_env: str = "production"
    test_mode: bool = False
    simulate: bool = False
    signer_type: str = "hot"

    solana_rpc_url: str = Field(
        default="https://mainnet.helius-rpc.com/?api-key=replace-me"
    )
    solana_rpc_url_fast: str | None = None
    solana_rpc_ws_url: str | None = None
    helius_api_key: str = ""
    helius_webhook_public_url: str = ""
    rpc_provider: str = "multi"
    wallet_pubkey: str | None = None

    trading: TradingSettings = Field(default_factory=TradingSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)

    # Legacy flat env (UPPER_CASE) — kept for existing .env / compose.env
    APP_ENV: str = "production"
    TEST_MODE: bool = False
    SIMULATE: bool = False
    SIGNER_TYPE: str = "hot"
    ALLOW_HOT_KEY_IN_PROD: bool = False
    DENY_DOT_ENV_TXT: bool = True
    ENFORCE_WALLET_PUBKEY: bool = True
    WALLET_PUBKEY: str | None = None

    RPC_PROVIDER: str = "multi"
    SOLANA_RPC_URL: str = "https://mainnet.helius-rpc.com/?api-key=replace-me"
    SOLANA_RPC_URL_FAST: str | None = None
    SOLANA_RPC_WS_URL: str | None = None
    HELIUS_API_KEY: str | None = None
    HELIUS_WEBHOOK_PUBLIC_URL: str | None = None
    ALLOW_PUBLIC_RPC_FALLBACK: bool = True
    RPC_RATE_PER_SEC: int = 6
    RPC_429_COOLDOWN_SEC: int = 60
    ENABLE_HELIUS_WEBHOOK: bool = True
    ENABLE_HELIUS_WEBHOOK_BACKRUN: bool = True

    MIN_PROFIT_USDC: float = 15.0
    MAX_DAILY_LOSS_USDC: float = 25.0
    MAX_SLIPPAGE_BPS: int = 35
    KILL_SWITCH_ON_LOSS: bool = True
    MEV_PROTECTION_ENABLED: bool = True
    LIVE_TRADING_CONFIRM: str = ""
    LIVE_TRADE_COOLDOWN_SECONDS: int = 150
    MAX_LIVE_TRADES_PER_HOUR: int = 4
    MAX_LIVE_TRADES_PER_DAY: int = 24
    MAX_DAILY_VOLUME_USDC_MICRO: int = 0
    MAX_SINGLE_TRADE_USDC_MICRO: int = 0

    CEX_DEX_PRIMARY_VENUE: str = "backpack"
    CEX_MIDCAPS: str = "BONK,WIF,JUP,PYTH,RAY,JTO,ORCA,POPCAT,W,DRIFT,GMT,MEW,SAMO,FIDA,FARTCOIN"
    CEX_MAX_MIDCAPS: int = 20
    CEX_DEX_INCLUDE_SOL: bool = True
    CEX_DEX_MIN_GROSS_SPREAD_BPS: int = 10
    CEX_DEX_MIN_NET_SPREAD_BPS: float = 4.0
    MIN_NET_PROFIT_BPS: int = 4
    CEX_DEX_EDGE_SAFETY_BPS: int = 0
    # Top-level direct access (fixes nested Pydantic alias / env sync)
    CEX_DEX_LOG_NEAR_MISSES: bool = True
    CEX_DEX_AGGRESSIVE_OPPORTUNITY_FILTER: bool = False
    CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS: float = 3.0
    CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS_GO_LIVE: float = 0.15
    CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC: float = 0.10
    CEX_DEX_ROUNDTRIP_SOFT_PASS_FACTOR: float = 0.72
    V2_MIN_NET_BPS: float = 1.0
    CEX_DEX_PROBE_USDC_MICRO: int = 12_000_000
    ENABLE_ONCHAIN_PROFIT_ASSERT: bool = True
    ONCHAIN_PROFIT_ASSERT_BPS: int = 12
    CEX_DEX_BRAIN_PRIORITY_BIAS: float = 80.0
    ONCHAIN_PROFIT_ASSERT_STRICT: bool = True
    CEX_DEX_USE_COMPONENT_COST_MODEL: bool = False

    CEX_DEX_CEX_FEE_ROUNDTRIP_BPS: int = 8
    CEX_DEX_JUPITER_LEG_FEE_BUFFER_BPS: int = 4
    CEX_DEX_EXECUTION_SLIPPAGE_BUFFER_BPS: int = 8
    CEX_DEX_WITHDRAWAL_LATENCY_BPS: int = 0
    CEX_DEX_STRATEGY_BASE_COST_BPS: float = 11.0
    CEX_DEX_FLASH_AMOUNT_USDC_MICRO: int = 150_000_000
    CEX_DEX_MAX_TRADE_USDC_MICRO: int = 450_000_000
    CEX_DEX_MIN_TRADE_USDC_MICRO: int = 30_000_000
    CEX_DEX_ORACLE_POLL_MIN_SEC: float = 1.0
    CEX_DEX_ORACLE_POLL_MAX_SEC: float = 5.0
    CEX_DEX_ORACLE_JITTER: bool = True
    CEX_DEX_DEPTH_UTILIZATION: float = 0.65

    DYNAMIC_AMOUNT: bool = True
    MIN_FLASH_USDC: int = 30_000
    MAX_FLASH_USDC: int = 500_000
    FLASH_SIZE_UTILIZATION: float = 0.68
    FLASH_SIZE_IMPACT_TOLERANCE_PCT: float = 0.42

    AI_APPROVE_MIN_CONFIDENCE: int = 72
    CEX_DEX_AI_CONFIDENCE_FLOOR: int = 84
    ENABLE_AI_CYCLE_BRAIN: bool = True
    AI_APPROVE_MIN_EST_NET_PCT: float = 0.15
    AI_PNL_CONFIDENCE_WINDOW_HOURS: int = 96
    ENHANCED_AI_TEMPERATURE: float = 0.0
    OPENAI_API_KEY: str | None = None

    JUPITER_QUOTE_URL: str = "https://api.jup.ag/swap/v1/quote"
    JUPITER_SWAP_URL: str = "https://api.jup.ag/swap/v1/swap"
    V2_EXECUTION_SLIPPAGE_BPS: int = 60
    JITO_TIP_FILL_RATE_TARGET: float = 0.40
    JITO_DYNAMIC_TIP: bool = True

    V2_MIN_USDC_BALANCE: float = 12.0
    V2_TARGET_BACKPACK_SOL: float = 0.65
    ENABLE_BACKPACK_AUTO_REPLENISH: bool = True

    METRICS_PROMETHEUS_PORT: int = 9091

    STRATEGY_PRIORITY_ORDER: Annotated[
        list[str],
        NoDecode,
        Field(
            default_factory=lambda: [
                "cex_dex",
                "dex_cex_reverse",
                "backrun",
                "collateral_swap",
                "liquidation",
            ]
        ),
    ]
    STRATEGY_PRIORITY_SCORE_BIAS: int = 30
    ENABLE_DAILY_INVENTORY_RECONCILE: bool = True
    ENABLE_COLLATERAL_RATE_ARB: bool = True
    COLLATERAL_MIN_SPREAD_BPS: int = 150
    COLLATERAL_FLASH_AMOUNT_USDC_MICRO: int = 50_000_000

    MAX_DRAWDOWN_PCT: float = 4.0
    INVENTORY_MAX_SOL: float = 45.0
    ENFORCE_MIN_SIMS_BEFORE_LIVE: bool = True
    MIN_SUCCESSFUL_SIMS_BEFORE_LIVE: int = 5
    CIRCUIT_BREAKER_FAIL_MAX: int = 5
    CIRCUIT_BREAKER_RESET_TIMEOUT: int = 60

    JITO_TIP_LAMPORTS: int = 120_000
    CEX_DEX_JITO_TIP_LAMPORTS: int = 120_000
    CEX_WITHDRAWAL_BUFFER_SEC: int = 22
    MAX_TIP_LAMPORTS: int = 500_000
    DYNAMIC_TIP_MULTIPLIER: float = 0.15

    MONITOR_POLL_SEC: int = 12
    ENABLE_BOT_HEALTH_SERVER: bool = True
    BOT_HEALTH_PORT: int = 8000
    BOT_HEALTH_HOST: str = "0.0.0.0"
    LOG_FORMAT: str = "json"
    WALLET_SAFETY_STATE_PATH: str = "logs/wallet_safety_state.json"

    SECRET_MANAGER: str = "local"
    SECRETS_ENCRYPTION: str = "sops"
    PRIVATE_KEY: str | None = None
    PRIVATE_KEY_CEX_DEX: str | None = None
    PRIVATE_KEY_FILE: str | None = None
    PRIVATE_KEY_CEX_DEX_FILE: str | None = None
    JUPITER_API_KEY_FILE: str | None = None
    BACKPACK_API_KEY: str | None = None
    BACKPACK_SECRET: str | None = None
    BACKPACK_API_KEY_FILE: str | None = None
    BACKPACK_SECRET_FILE: str | None = None
    OPENAI_API_KEY_FILE: str | None = None
    HELIUS_API_KEY_FILE: str | None = None

    ENABLE_PHOENIX_V1: bool = False
    PHOENIX_SOL_USDC_MARKET: str = "4DoNfFBfF7UokCC2FQzriy7yHK6DY6NVdYpuekQ5pRgg"
    PHOENIX_PROBE_USDC_MICRO: int = 12_000_000

    KAMINO_MARKET_PUBKEY: str | None = None
    KAMINO_LENDING_MARKET_PUBKEY: str | None = None

    @field_validator("STRATEGY_PRIORITY_ORDER", mode="before")
    @classmethod
    def parse_strategy_priority_order(cls, v: Any) -> list[str]:
        default = [
            "cex_dex",
            "dex_cex_reverse",
            "backrun",
            "collateral_swap",
            "liquidation",
        ]
        if v is None:
            return default
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return default
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed]
                except json.JSONDecodeError:
                    pass
            return [part.strip() for part in s.split(",") if part.strip()]
        return default

    @field_validator("solana_rpc_url")
    @classmethod
    def validate_rpc(cls, v: str) -> str:
        if not v or "your" in v.lower() or "replace-me" in v:
            raise ValueError("Set real paid RPC URL in SOLANA_RPC_URL")
        return v

    @model_validator(mode="after")
    def sync_core_and_nested(self) -> Settings:
        """Mirror legacy UPPER_CASE env into lowercase + nested trading/risk."""
        object.__setattr__(self, "app_env", self.APP_ENV)
        object.__setattr__(self, "test_mode", self.TEST_MODE)
        object.__setattr__(self, "simulate", self.SIMULATE)
        object.__setattr__(self, "signer_type", self.SIGNER_TYPE)
        object.__setattr__(self, "rpc_provider", self.RPC_PROVIDER)
        object.__setattr__(self, "wallet_pubkey", self.WALLET_PUBKEY)

        rpc = self.SOLANA_RPC_URL or self.solana_rpc_url
        object.__setattr__(self, "solana_rpc_url", rpc)
        object.__setattr__(self, "SOLANA_RPC_URL", rpc)
        object.__setattr__(
            self,
            "solana_rpc_url_fast",
            self.SOLANA_RPC_URL_FAST or self.solana_rpc_url_fast,
        )
        object.__setattr__(
            self,
            "solana_rpc_ws_url",
            self.SOLANA_RPC_WS_URL or self.solana_rpc_ws_url,
        )
        object.__setattr__(
            self,
            "helius_api_key",
            self.HELIUS_API_KEY or self.helius_api_key or "",
        )
        object.__setattr__(
            self,
            "helius_webhook_public_url",
            self.HELIUS_WEBHOOK_PUBLIC_URL or self.helius_webhook_public_url or "",
        )

        max_flash_usdc = float(self.MAX_FLASH_USDC)
        v2_max_raw = (os.getenv("V2_MAX_FLASH_USDC") or "").strip()
        if v2_max_raw:
            try:
                max_flash_usdc = float(v2_max_raw)
            except ValueError:
                pass

        trading = TradingSettings(
            min_net_profit_bps=int(self.MIN_NET_PROFIT_BPS),
            cex_dex_min_gross_spread_bps=int(self.CEX_DEX_MIN_GROSS_SPREAD_BPS),
            cex_dex_min_net_spread_bps=float(self.CEX_DEX_MIN_NET_SPREAD_BPS),
            ai_approve_min_confidence=float(self.AI_APPROVE_MIN_CONFIDENCE),
            cex_dex_ai_confidence_floor=float(self.CEX_DEX_AI_CONFIDENCE_FLOOR),
            live_trade_cooldown_seconds=int(self.LIVE_TRADE_COOLDOWN_SECONDS),
            max_live_trades_per_hour=int(self.MAX_LIVE_TRADES_PER_HOUR),
            dynamic_amount=bool(self.DYNAMIC_AMOUNT),
            min_flash_usdc=float(self.MIN_FLASH_USDC),
            max_flash_usdc=max_flash_usdc,
            flash_size_utilization=float(self.FLASH_SIZE_UTILIZATION),
            max_drawdown_pct=float(self.MAX_DRAWDOWN_PCT),
            max_inventory_sol=float(self.INVENTORY_MAX_SOL),
            volatility_filter_bps=int(
                os.getenv("VOLATILITY_FILTER_BPS", str(self.trading.volatility_filter_bps))
            ),
            cex_dex_depth_utilization=float(self.CEX_DEX_DEPTH_UTILIZATION),
            cex_dex_strategy_base_cost_bps=int(self.CEX_DEX_STRATEGY_BASE_COST_BPS),
            cex_dex_log_near_misses=bool(self.CEX_DEX_LOG_NEAR_MISSES),
        )
        object.__setattr__(self, "trading", trading)

        risk = RiskSettings(
            max_daily_loss_usdc=float(self.MAX_DAILY_LOSS_USDC),
            circuit_breaker_loss_streak=int(
                os.getenv(
                    "CIRCUIT_BREAKER_LOSS_STREAK",
                    str(self.risk.circuit_breaker_loss_streak),
                )
            ),
            circuit_breaker_loss_usd=float(
                os.getenv("CIRCUIT_BREAKER_LOSS_USD", str(self.risk.circuit_breaker_loss_usd))
            ),
            enable_onchain_profit_assert=bool(
                os.getenv("ENABLE_ONCHAIN_PROFIT_ASSERT", "true").lower()
                in ("1", "true", "yes")
            ),
            onchain_profit_assert_bps=int(
                os.getenv(
                    "ONCHAIN_PROFIT_ASSERT_BPS",
                    str(getattr(self, "ONCHAIN_PROFIT_ASSERT_BPS", 20)),
                )
            ),
        )
        object.__setattr__(self, "risk", risk)
        return self

    @property
    def cex_dex_log_near_misses(self) -> bool:
        """``CEX_DEX_LOG_NEAR_MISSES`` env (top-level; synced into ``trading``)."""
        return bool(self.CEX_DEX_LOG_NEAR_MISSES)

    def is_production(self) -> bool:
        return str(self.app_env).lower() == "production"

    def validate_safety(self) -> None:
        if self.is_production() and self.ALLOW_HOT_KEY_IN_PROD:
            raise ValueError("Hot key not allowed in production!")
        if self.trading.cex_dex_min_net_spread_bps < 3:
            print("⚠️  Warning: Very low net spread threshold — win rate may suffer")

    @property
    def live_trading_confirm_enabled(self) -> bool:
        return os.getenv("LIVE_TRADING_CONFIRM", "").strip().upper() == "YES"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    settings = Settings()
    settings.validate_safety()
    return settings


def bootstrap_config() -> Settings:
    """Load and validate settings (call early in main)."""
    get_settings.cache_clear()
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    settings = get_settings()
    os.environ.setdefault("PYTHONPATH", os.getcwd())
    print(
        f"✅ Settings loaded | ENV={settings.app_env} | SIGNER={settings.signer_type}"
    )
    if not (settings.WALLET_PUBKEY or settings.wallet_pubkey):
        print("⚠️  WALLET_PUBKEY not set!")
    return settings


class _SettingsProxy:
    """Lazy settings access so imports after bootstrap_config() see fresh env."""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_settings(), name)


# Module-level alias for legacy imports (do not cache a snapshot at import time).
settings: Settings = _SettingsProxy()  # type: ignore[assignment]
