"""Unified config for extended meme lanes (copy, migration, filter, hybrid MEV)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv_list(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


class SmartMoneyCopySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "compose.env"), extra="ignore")

    enabled: bool = Field(default=False, validation_alias="ENABLE_SMART_MONEY_COPY")
    simulate: bool = Field(default=True, validation_alias="SMART_MONEY_COPY_SIMULATE")
    min_copy_confidence: float = Field(default=65.0, validation_alias="SMART_MONEY_MIN_COPY_CONFIDENCE")
    max_copy_sol: float = Field(default=0.8, validation_alias="SMART_MONEY_MAX_COPY_SOL")
    leader_size_mult: float = Field(default=0.6, validation_alias="SMART_MONEY_LEADER_SIZE_MULT")
    min_win_rate: float = Field(default=0.65, validation_alias="SMART_MONEY_MIN_WIN_RATE")
    min_pnl_mult: float = Field(default=2.5, validation_alias="SMART_MONEY_MIN_PNL_MULT")
    poll_interval_sec: float = Field(default=12.0, validation_alias="SMART_MONEY_POLL_INTERVAL_SEC")
    tracked_wallets_raw: str = Field(default="", validation_alias="SMART_MONEY_TRACKED_WALLETS")

    @property
    def tracked_wallets(self) -> list[str]:
        return _csv_list(self.tracked_wallets_raw)


class MigrationSniperSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "compose.env"), extra="ignore")

    enabled: bool = Field(default=False, validation_alias="ENABLE_MIGRATION_SNIPER")
    simulate: bool = Field(default=True, validation_alias="MIGRATION_SNIPER_SIMULATE")
    min_safety_score: float = Field(default=85.0, validation_alias="MIGRATION_SNIPER_MIN_SAFETY")
    min_ai_score: float = Field(default=78.0, validation_alias="MIGRATION_SNIPER_MIN_AI_SCORE")
    max_trade_sol: float = Field(default=1.0, validation_alias="MIGRATION_SNIPER_MAX_TRADE_SOL")
    max_age_minutes: int = Field(default=45, validation_alias="MIGRATION_SNIPER_MAX_AGE_MIN")
    poll_interval_sec: float = Field(default=2.5, validation_alias="MIGRATION_SNIPER_POLL_SEC")


class FilterDiscoverySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "compose.env"), extra="ignore")

    enabled: bool = Field(default=False, validation_alias="ENABLE_FILTER_DISCOVERY")
    simulate: bool = Field(default=True, validation_alias="FILTER_DISCOVERY_SIMULATE")
    min_score: float = Field(default=82.0, validation_alias="FILTER_DISCOVERY_MIN_SCORE")
    min_liq_usd: float = Field(default=15000.0, validation_alias="FILTER_DISCOVERY_MIN_LIQ_USD")
    min_vol_5m_bps: int = Field(default=900, validation_alias="FILTER_DISCOVERY_MIN_VOL_5M_BPS")
    max_dev_pct: float = Field(default=8.0, validation_alias="FILTER_DISCOVERY_MAX_DEV_PCT")
    min_social: int = Field(default=50, validation_alias="FILTER_DISCOVERY_MIN_SOCIAL")
    require_burned_lp: bool = Field(default=True, validation_alias="FILTER_DISCOVERY_REQUIRE_LP_BURNED")
    max_trade_sol: float = Field(default=1.0, validation_alias="FILTER_DISCOVERY_MAX_TRADE_SOL")
    poll_interval_sec: float = Field(default=1.2, validation_alias="FILTER_DISCOVERY_POLL_SEC")


class HybridMevMemeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "compose.env"), extra="ignore")

    enabled: bool = Field(default=False, validation_alias="ENABLE_HYBRID_MEV_MEME")
    simulate: bool = Field(default=True, validation_alias="HYBRID_MEV_MEME_SIMULATE")
    min_m5_buy_usd: float = Field(default=8000.0, validation_alias="HYBRID_MEV_MIN_M5_BUY_USD")
    jito_tip_mult: float = Field(default=1.7, validation_alias="HYBRID_MEV_JITO_TIP_MULT")
    max_trade_sol: float = Field(default=0.9, validation_alias="HYBRID_MEV_MAX_TRADE_SOL")


@lru_cache(maxsize=1)
def get_smart_money_settings() -> SmartMoneyCopySettings:
    return SmartMoneyCopySettings()


@lru_cache(maxsize=1)
def get_migration_sniper_settings() -> MigrationSniperSettings:
    return MigrationSniperSettings()


@lru_cache(maxsize=1)
def get_filter_discovery_settings() -> FilterDiscoverySettings:
    return FilterDiscoverySettings()


@lru_cache(maxsize=1)
def get_hybrid_mev_settings() -> HybridMevMemeSettings:
    return HybridMevMemeSettings()
