"""Meme sniping lane settings (simulate-first)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MemeSnipingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "compose.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=False, validation_alias="ENABLE_MEME_SNIPING")

    min_liquidity_usd: float = Field(
        default=12000.0, validation_alias="MEME_SNIPING_MIN_LIQUIDITY_USD"
    )
    min_volatility_bps: int = Field(default=800, validation_alias="MEME_SNIPING_MIN_VOL_BPS")
    min_social_score: int = Field(
        default=35, validation_alias="MEME_SNIPING_MIN_SOCIAL_SCORE"
    )

    max_trade_sol: float = Field(default=1.8, validation_alias="MEME_SNIPING_MAX_TRADE_SOL")
    max_loss_bps: int = Field(default=-50, validation_alias="MEME_SNIPING_MAX_LOSS_BPS")
    max_daily_loss_usd: float = Field(
        default=35.0, validation_alias="MEME_SNIPING_MAX_DAILY_LOSS_USD"
    )

    profit_target_1_bps: int = Field(default=50, validation_alias="MEME_SNIPING_TP1_BPS")
    profit_target_2_bps: int = Field(default=80, validation_alias="MEME_SNIPING_TP2_BPS")
    profit_target_3_bps: int = Field(default=120, validation_alias="MEME_SNIPING_TP3_BPS")

    jito_tip_mult: float = Field(default=1.5, validation_alias="MEME_SNIPING_JITO_TIP_MULT")
    ai_min_confidence: float = Field(
        default=76.0, validation_alias="MEME_SNIPING_AI_CONFIDENCE"
    )
    max_hold_minutes: int = Field(
        default=18, validation_alias="MEME_SNIPING_MAX_HOLD_MINUTES"
    )

    simulate: bool = Field(default=True, validation_alias="MEME_SNIPING_SIMULATE")
    priority_bias: int = Field(default=68, validation_alias="MEME_SNIPING_PRIORITY_BIAS")


@lru_cache(maxsize=1)
def get_meme_sniping_settings() -> MemeSnipingSettings:
    return MemeSnipingSettings()


meme_sniping_settings = get_meme_sniping_settings()
