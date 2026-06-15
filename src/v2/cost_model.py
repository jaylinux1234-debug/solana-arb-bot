"""v2.4.2 reverse-lane cost model (size + volatility aware)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.v2.config import V2Config

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def v2_use_realistic_cost_model() -> bool:
    raw = os.getenv("V2_USE_REALISTIC_COST_MODEL", "true")
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class CostModel:
    """Realistic, size + volatility aware cost model for v2.4.2."""

    base_cost_bps: float = 6.5
    jito_tip_bps: float = 1.1
    withdrawal_buffer_bps: float = 2.0
    size_impact_exponent: float = 1.25
    size_ref_usdc: float = 15.0
    size_impact_linear_bps: float = 1.8
    vol_multiplier: float = 1.0
    vol_penalty_threshold_pct: float = 0.6
    vol_penalty_slope: float = 8.0

    @classmethod
    def from_config(cls, config: V2Config | None = None) -> CostModel:
        if config is not None:
            return cls(
                base_cost_bps=float(config.base_cost_bps),
                jito_tip_bps=float(config.jito_tip_bps),
                withdrawal_buffer_bps=float(config.slippage_buffer_bps),
                size_ref_usdc=_env_float("V2_COST_REF_USDC", 15.0),
                size_impact_exponent=_env_float("V2_COST_SIZE_IMPACT_EXPONENT", 1.25),
                size_impact_linear_bps=_env_float("V2_COST_SIZE_IMPACT_LINEAR_BPS", 1.8),
                vol_multiplier=_env_float("V2_COST_VOL_MULTIPLIER", 1.0),
                vol_penalty_threshold_pct=_env_float(
                    "V2_VOL_LOW_THRESHOLD_PCT",
                    float(config.vol_low_threshold_pct),
                ),
                vol_penalty_slope=_env_float("V2_COST_VOL_PENALTY_SLOPE", 8.0),
            )
        return cls(
            base_cost_bps=_env_float(
                "V2_BASE_COST_BPS",
                _env_float("V2_COST_BASE_BPS", 5.5),
            ),
            jito_tip_bps=_env_float("V2_COST_JITO_TIP_BPS", 1.1),
            withdrawal_buffer_bps=_env_float(
                "V2_SLIPPAGE_BUFFER_BPS",
                _env_float("V2_COST_JUPITER_SLIPPAGE_BPS", 2.0),
            ),
            size_impact_exponent=_env_float("V2_COST_SIZE_IMPACT_EXPONENT", 1.25),
            size_ref_usdc=_env_float("V2_COST_REF_USDC", 15.0),
            size_impact_linear_bps=_env_float("V2_COST_SIZE_IMPACT_LINEAR_BPS", 1.8),
            vol_multiplier=_env_float("V2_COST_VOL_MULTIPLIER", 1.0),
            vol_penalty_threshold_pct=_env_float("V2_VOL_LOW_THRESHOLD_PCT", 0.6),
            vol_penalty_slope=_env_float("V2_COST_VOL_PENALTY_SLOPE", 8.0),
        )

    def calculate_net_bps(
        self,
        gross_bps: float,
        size_usdc: float,
        vol_pct: float = 0.0,
        *,
        wallet_sol: float = 0.0,
        cex_sol: float = 0.0,
    ) -> float:
        """Calculate realistic net after all costs."""
        from src.core.cost_model import get_advanced_cost_model, use_advanced_cost_model

        if use_advanced_cost_model():
            trade_micro = max(1, int(max(0.0, size_usdc) * 1_000_000))
            rc = get_advanced_cost_model().calculate_roundtrip(
                float(gross_bps),
                trade_micro,
                vol_5m_pct=float(vol_pct),
                wallet_sol=float(wallet_sol),
                cex_sol=float(cex_sol),
                is_reverse_path=True,
            )
            logger.debug(
                "ADV_COST_MODEL reverse | gross=%s size=%s vol=%s breakdown=%s net=%s",
                gross_bps,
                size_usdc,
                vol_pct,
                rc.breakdown,
                rc.net_bps,
            )
            return rc.net_bps

        size_factor = (max(0.0, size_usdc) / max(1.0, self.size_ref_usdc)) ** (
            self.size_impact_exponent
        )
        size_impact_bps = self.size_impact_linear_bps * size_factor
        vol_penalty = max(
            0.0,
            (vol_pct - self.vol_penalty_threshold_pct)
            * self.vol_penalty_slope
            * self.vol_multiplier,
        )
        total_cost_bps = (
            self.base_cost_bps
            + self.jito_tip_bps
            + self.withdrawal_buffer_bps
            + size_impact_bps
            + vol_penalty
        )
        net_bps = round(float(gross_bps) - total_cost_bps, 3)
        logger.debug(
            "COST_MODEL | gross=%s size=%s vol_pct=%s total_cost=%s net=%s",
            gross_bps,
            size_usdc,
            vol_pct,
            round(total_cost_bps, 2),
            net_bps,
        )
        return net_bps

    def get_execution_slippage_bps(self, size_usdc: float) -> int:
        """Higher slippage for larger live execution."""
        size_bonus = int(8 * (max(0.0, size_usdc) / 10.0))
        return max(50, 45 + size_bonus)

    def calculate_net_bps_compat(
        self,
        gross_bps: float,
        size_usdc_micro: int = 0,
        *,
        direction: str = "dex_cheap",
        size_usdc: float | None = None,
        vol_pct: float = 0.0,
        wallet_sol: float = 0.0,
        cex_sol: float = 0.0,
    ) -> float:
        """Backward-compatible entry for lane / gates (micro + optional vol)."""
        _ = direction
        usdc = (
            float(size_usdc)
            if size_usdc is not None
            else max(0.0, size_usdc_micro) / 1_000_000.0
        )
        return self.calculate_net_bps(
            gross_bps,
            usdc,
            vol_pct,
            wallet_sol=wallet_sol,
            cex_sol=cex_sol,
        )


def calculate_net_bps(
    gross_bps: float,
    size_usdc_micro: int,
    *,
    direction: str = "dex_cheap",
    config: V2Config | None = None,
    vol_pct: float = 0.0,
) -> float:
    """
    Modeled net for reverse arb (v2.4.2).

    Set ``V2_USE_REALISTIC_COST_MODEL=false`` for legacy ``net_spread_bps_after_costs``.
    """
    if not v2_use_realistic_cost_model():
        from src.strategies.cex_dex_core import net_spread_bps_after_costs

        return net_spread_bps_after_costs(
            gross_bps,
            size_usdc_micro,
            direction=direction,
        )
    model = CostModel.from_config(config)
    return model.calculate_net_bps_compat(
        gross_bps,
        size_usdc_micro,
        direction=direction,
        vol_pct=vol_pct,
    )


def default_cost_model() -> CostModel:
    """Env-backed singleton (call after ``bootstrap_config`` / ``V2Config.from_env``)."""
    return CostModel.from_config(None)


def refresh_cost_model(config: V2Config | None = None) -> CostModel:
    """Reload module ``cost_model`` from env or ``V2Config`` (call from ``main``)."""
    global cost_model
    cost_model = CostModel.from_config(config)
    return cost_model


cost_model = default_cost_model()
