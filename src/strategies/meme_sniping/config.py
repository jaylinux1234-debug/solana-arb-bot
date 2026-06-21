"""Meme sniping lane settings v2 (Alchemy-optimized, simulate-first)."""

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

    enabled: bool = Field(default=True, validation_alias="ENABLE_MEME_SNIPING")
    use_alchemy: bool = Field(default=True, validation_alias="MEME_SNIPING_USE_ALCHEMY")
    use_dex_price: bool = Field(default=True, validation_alias="MEME_SNIPING_USE_DEX_PRICE")

    min_liquidity_usd: float = Field(
        default=10000.0, validation_alias="MEME_SNIPING_MIN_LIQUIDITY_USD"
    )
    min_volatility_bps: int = Field(default=700, validation_alias="MEME_SNIPING_MIN_VOL_BPS")
    min_social_score: int = Field(
        default=30, validation_alias="MEME_SNIPING_MIN_SOCIAL_SCORE"
    )

    max_trade_sol: float = Field(default=1.6, validation_alias="MEME_SNIPING_MAX_TRADE_SOL")
    max_loss_bps: int = Field(default=-50, validation_alias="MEME_SNIPING_MAX_LOSS_BPS")
    max_daily_loss_usd: float = Field(
        default=30.0, validation_alias="MEME_SNIPING_MAX_DAILY_LOSS_USD"
    )

    profit_target_1_bps: int = Field(default=50, validation_alias="MEME_SNIPING_TP1_BPS")
    profit_target_2_bps: int = Field(default=85, validation_alias="MEME_SNIPING_TP2_BPS")
    profit_target_3_bps: int = Field(default=130, validation_alias="MEME_SNIPING_TP3_BPS")

    jito_tip_mult: float = Field(default=1.55, validation_alias="MEME_SNIPING_JITO_TIP_MULT")
    ai_min_confidence: float = Field(
        default=75.0, validation_alias="MEME_SNIPING_AI_CONFIDENCE"
    )
    max_hold_minutes: int = Field(
        default=20, validation_alias="MEME_SNIPING_MAX_HOLD_MINUTES"
    )

    stop_grace_sec: int = Field(default=45, validation_alias="MEME_SNIPING_STOP_GRACE_SEC")
    stop_confirm_polls: int = Field(
        default=2, validation_alias="MEME_SNIPING_STOP_CONFIRM_POLLS"
    )
    mint_cooldown_minutes: int = Field(
        default=30, validation_alias="MEME_SNIPING_MINT_COOLDOWN_MIN"
    )
    poll_interval_sec: float = Field(
        default=4.0, validation_alias="MEME_SNIPING_POLL_INTERVAL_SEC"
    )

    simulate: bool = Field(default=True, validation_alias="MEME_SNIPING_SIMULATE")
    priority_bias: int = Field(default=68, validation_alias="MEME_SNIPING_PRIORITY_BIAS")


@lru_cache(maxsize=1)
def get_meme_sniping_settings() -> MemeSnipingSettings:
    return MemeSnipingSettings()


meme_sniping_settings = get_meme_sniping_settings()
