"""Meme sniping lane settings v3 (production filters, trailing stop, partial TPs)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_csv_floats(raw: str, defaults: list[float]) -> list[float]:
    text = (raw or "").strip()
    if not text:
        return defaults
    out: list[float] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            continue
    return out or defaults


def _parse_csv_str(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    return [p.strip() for p in text.split(",") if p.strip()]


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
        default=15000.0, validation_alias="MEME_SNIPING_MIN_LIQUIDITY_USD"
    )
    min_volatility_bps: int = Field(default=800, validation_alias="MEME_SNIPING_MIN_VOL_BPS")
    min_social_score: int = Field(
        default=45, validation_alias="MEME_SNIPING_MIN_SOCIAL_SCORE"
    )

    max_trade_sol: float = Field(default=1.2, validation_alias="MEME_SNIPING_MAX_TRADE_SOL")
    max_loss_bps: int = Field(default=-45, validation_alias="MEME_SNIPING_MAX_LOSS_BPS")
    max_daily_loss_usd: float = Field(
        default=25.0, validation_alias="MEME_SNIPING_MAX_DAILY_LOSS_USD"
    )

    profit_target_1_bps: int = Field(default=50, validation_alias="MEME_SNIPING_TP1_BPS")
    profit_target_2_bps: int = Field(default=85, validation_alias="MEME_SNIPING_TP2_BPS")
    profit_target_3_bps: int = Field(default=130, validation_alias="MEME_SNIPING_TP3_BPS")
    profit_target_4_bps: int = Field(default=200, validation_alias="MEME_SNIPING_TP4_BPS")
    tp_levels_bps_raw: str = Field(
        default="", validation_alias="MEME_SNIPING_TP_LEVELS_BPS"
    )
    tp_partial_fractions_raw: str = Field(
        default="0.4,0.3,0.2,0.1", validation_alias="MEME_SNIPING_TP_PARTIAL_FRACTIONS"
    )

    jito_tip_mult: float = Field(default=1.65, validation_alias="MEME_SNIPING_JITO_TIP_MULT")
    ai_min_confidence: float = Field(
        default=78.0, validation_alias="MEME_SNIPING_AI_CONFIDENCE"
    )
    ensemble_min_score: float = Field(
        default=72.0, validation_alias="MEME_SNIPING_ENSEMBLE_MIN_SCORE"
    )
    max_hold_minutes: int = Field(
        default=15, validation_alias="MEME_SNIPING_MAX_HOLD_MINUTES"
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

    max_dev_wallet_pct: float = Field(
        default=8.0, validation_alias="MEME_SNIPING_MAX_DEV_WALLET_PCT"
    )
    require_lp_burned: bool = Field(
        default=True, validation_alias="MEME_SNIPING_REQUIRE_LP_BURNED"
    )
    min_holder_count: int = Field(
        default=150, validation_alias="MEME_SNIPING_MIN_HOLDER_COUNT"
    )
    max_sell_tax_pct: float = Field(
        default=5.0, validation_alias="MEME_SNIPING_MAX_SELL_TAX_PCT"
    )
    validator_min_safety_score: float = Field(
        default=85.0, validation_alias="MEME_SNIPING_VALIDATOR_MIN_SAFETY"
    )
    blacklist_tokens_raw: str = Field(
        default="", validation_alias="MEME_SNIPING_BLACKLIST_TOKENS"
    )

    enable_trailing_stop: bool = Field(
        default=True, validation_alias="MEME_SNIPING_ENABLE_TRAILING_STOP"
    )
    trailing_stop_bps: float = Field(
        default=35.0, validation_alias="MEME_SNIPING_TRAILING_STOP_BPS"
    )
    trailing_arm_bps: float = Field(
        default=60.0, validation_alias="MEME_SNIPING_TRAILING_ARM_BPS"
    )

    simulate: bool = Field(default=True, validation_alias="MEME_SNIPING_SIMULATE")
    priority_bias: int = Field(default=68, validation_alias="MEME_SNIPING_PRIORITY_BIAS")

    @property
    def tp_levels_bps(self) -> list[int]:
        defaults = [
            self.profit_target_1_bps,
            self.profit_target_2_bps,
            self.profit_target_3_bps,
            self.profit_target_4_bps,
        ]
        parsed = _parse_csv_floats(self.tp_levels_bps_raw, [float(x) for x in defaults])
        return [int(x) for x in parsed]

    @property
    def tp_partial_fractions(self) -> list[float]:
        return _parse_csv_floats(self.tp_partial_fractions_raw, [0.4, 0.3, 0.2, 0.1])

    @property
    def blacklist_tokens(self) -> list[str]:
        return _parse_csv_str(self.blacklist_tokens_raw)

    # Backward-compatible single TP accessors
    @property
    def profit_target_1_bps_compat(self) -> int:
        levels = self.tp_levels_bps
        return levels[0] if levels else self.profit_target_1_bps

    @field_validator("blacklist_tokens_raw", mode="before")
    @classmethod
    def _coerce_blacklist(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v)


@lru_cache(maxsize=1)
def get_meme_sniping_settings() -> MemeSnipingSettings:
    return MemeSnipingSettings()


meme_sniping_settings = get_meme_sniping_settings()
