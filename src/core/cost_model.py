"""Advanced roundtrip cost model — size, volatility, inventory, and PATH-aware."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class RoundtripCost:
    gross_bps: float
    cex_fee_bps: float = 20.0
    jupiter_leg_bps: float = 15.0
    slippage_bps: float = 60.0
    kamino_flash_bps: float = 5.0
    jito_tip_bps: float = 1.5
    withdrawal_latency_bps: float = 25.0
    vol_penalty_bps: float = 0.0
    inventory_penalty_bps: float = 0.0
    total_cost_bps: float = 0.0
    net_bps: float = 0.0
    size_mult: float = 1.0
    breakdown: dict[str, float] = field(default_factory=dict)
    is_reverse_path: bool = True


@dataclass
class BackrunEstimate:
    """Modeled backrun edge after multi-leg Jupiter + Jito drag."""

    gross_bps: float
    net_bps: float
    profit_usd: float
    total_cost_bps: float
    trade_usdc_micro: int
    usdc_out_micro: int = 0


class AdvancedCostModel:
    """Component-based roundtrip drag. Optimized for reverse/hot-wallet flow."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        ref_usdc_dollars = _env_float(
            "CEX_DEX_SIZE_IMPACT_REF_USDC",
            _env_float("V2_COST_REF_USDC", 30.0),
        )
        self.base_config: dict[str, float] = {
            "cex_fee_roundtrip": _env_float("CEX_DEX_CEX_FEE_ROUNDTRIP_BPS", 18.0),
            "jupiter_buffer": _env_float("CEX_DEX_JUPITER_LEG_FEE_BUFFER_BPS", 12.0),
            "exec_slippage_base": _env_float(
                "CEX_DEX_EXECUTION_SLIPPAGE_BUFFER_BPS", 38.0
            ),
            "kamino_flash": _env_float("CEX_DEX_KAMINO_FLASH_FEE_BPS", 4.5),
            "jito_tip": _env_float("COST_JITO_TIP_BPS", 1.2),
            "withdrawal_latency_sec": _env_float("CEX_DEX_WITHDRAWAL_BUFFER_SEC", 8.0),
            "withdrawal_latency_per_sec": _env_float(
                "CEX_DEX_WITHDRAWAL_LATENCY_BPS_PER_SEC", 0.4
            ),
            "withdrawal_latency_bps_fixed": _env_float(
                "CEX_DEX_WITHDRAWAL_LATENCY_BPS", -1.0
            ),
            "vol_penalty_slope": _env_float("COST_VOL_PENALTY_SLOPE", 6.5),
            "size_impact_exponent": _env_float("CEX_DEX_SIZE_IMPACT_EXPONENT", 1.12),
            "size_ref_usdc_micro": ref_usdc_dollars * 1_000_000,
            "inventory_sol_floor": _env_float("COST_INVENTORY_SOL_FLOOR", 0.25),
            "inventory_penalty_bps": _env_float("COST_INVENTORY_PENALTY_BPS", 8.0),
        }
        if config:
            self.base_config.update(
                {k: float(v) for k, v in config.items() if v is not None}
            )
        self._apply_go_live_overrides()

    def _apply_go_live_overrides(self) -> None:
        if not _env_bool("GO_LIVE_SMALL_ACCOUNT", False):
            return
        self.base_config.update(
            {
                "inventory_sol_floor": _env_float(
                    "COST_INVENTORY_SOL_FLOOR_GO_LIVE", 0.18
                ),
                "inventory_penalty_bps": _env_float(
                    "COST_INVENTORY_PENALTY_BPS_GO_LIVE", 5.0
                ),
                "exec_slippage_base": self.base_config["exec_slippage_base"] * 0.88,
                "withdrawal_latency_per_sec": 0.25,
            }
        )

    def _trade_usdc_micro(self, trade_usdc: int | float) -> int:
        raw = float(trade_usdc)
        if raw >= 1_000_000:
            return int(raw)
        return max(1, int(raw * 1_000_000))

    def calculate_roundtrip(
        self,
        gross_bps: float,
        trade_usdc: int | float,
        cex_spread_bps: float = 0.0,
        vol_5m_pct: float = 0.8,
        wallet_sol: float = 0.0,
        cex_sol: float = 0.0,
        is_reverse_path: bool = True,
    ) -> RoundtripCost:
        _ = cex_spread_bps
        trade_micro = self._trade_usdc_micro(trade_usdc)
        ref_micro = max(1.0, self.base_config["size_ref_usdc_micro"])
        size_mult = (trade_micro / ref_micro) ** self.base_config["size_impact_exponent"]

        cost = RoundtripCost(
            gross_bps=float(gross_bps),
            size_mult=size_mult,
            is_reverse_path=is_reverse_path,
        )

        cost.cex_fee_bps = self.base_config["cex_fee_roundtrip"]
        cost.jupiter_leg_bps = self.base_config["jupiter_buffer"]
        cost.slippage_bps = self.base_config["exec_slippage_base"] * size_mult
        cost.kamino_flash_bps = self.base_config["kamino_flash"]
        cost.jito_tip_bps = self.base_config["jito_tip"]

        if is_reverse_path:
            cost.withdrawal_latency_bps = 5.0
        else:
            fixed = self.base_config.get("withdrawal_latency_bps_fixed", -1.0)
            if fixed >= 0:
                cost.withdrawal_latency_bps = fixed
            else:
                latency_sec = self.base_config["withdrawal_latency_sec"]
                cost.withdrawal_latency_bps = (
                    latency_sec * self.base_config["withdrawal_latency_per_sec"]
                )

        cost.vol_penalty_bps = (
            max(0.0, float(vol_5m_pct)) * self.base_config["vol_penalty_slope"]
        )

        total_sol = float(wallet_sol) + float(cex_sol)
        if total_sol < self.base_config["inventory_sol_floor"]:
            cost.inventory_penalty_bps = self.base_config["inventory_penalty_bps"]

        cost.total_cost_bps = (
            cost.cex_fee_bps
            + cost.jupiter_leg_bps
            + cost.slippage_bps
            + cost.kamino_flash_bps
            + cost.jito_tip_bps
            + cost.withdrawal_latency_bps
            + cost.vol_penalty_bps
            + cost.inventory_penalty_bps
        )
        cost.net_bps = round(float(gross_bps) - cost.total_cost_bps, 3)

        cost.breakdown = {
            "cex_fee": cost.cex_fee_bps,
            "jupiter": cost.jupiter_leg_bps,
            "slippage": cost.slippage_bps,
            "kamino": cost.kamino_flash_bps,
            "jito": cost.jito_tip_bps,
            "withdrawal_latency": cost.withdrawal_latency_bps,
            "vol_penalty": cost.vol_penalty_bps,
            "inventory": cost.inventory_penalty_bps,
            "total_cost": cost.total_cost_bps,
            "is_reverse_path": float(cost.is_reverse_path),
        }
        return cost

    def estimate_backrun(
        self,
        quotes: dict[str, Any],
        trade_usdc_micro: int,
    ) -> BackrunEstimate:
        """Model 3-leg USDC→mid→SOL→USDC backrun from Jupiter quote legs."""
        usdc_in = max(1, int(trade_usdc_micro))
        q3 = quotes.get("quote3_sol_to_usdc") or {}
        try:
            usdc_out = int(q3.get("outAmount") or 0)
        except (TypeError, ValueError):
            usdc_out = 0

        if usdc_out <= 0:
            return BackrunEstimate(0.0, 0.0, 0.0, 0.0, usdc_in, 0)

        gross_bps = ((usdc_out - usdc_in) / usdc_in) * 10_000.0
        rc = self.calculate_roundtrip(
            gross_bps=gross_bps,
            trade_usdc=usdc_in,
            vol_5m_pct=0.0,
            is_reverse_path=True,
        )
        extra_legs_bps = _env_float("BACKRUN_EXTRA_JUPITER_LEG_BPS", 24.0)
        total_cost_bps = rc.total_cost_bps + extra_legs_bps
        net_bps = gross_bps - total_cost_bps
        net_usdc_micro = usdc_out - usdc_in - int(usdc_in * total_cost_bps / 10_000.0)
        profit_usd = net_usdc_micro / 1_000_000.0

        return BackrunEstimate(
            gross_bps=round(gross_bps, 3),
            net_bps=round(net_bps, 3),
            profit_usd=round(profit_usd, 4),
            total_cost_bps=round(total_cost_bps, 3),
            trade_usdc_micro=usdc_in,
            usdc_out_micro=usdc_out,
        )

    def total_cost_bps(
        self,
        trade_usdc_micro: int,
        *,
        vol_5m_pct: float = 0.8,
        wallet_sol: float = 0.0,
        cex_sol: float = 0.0,
        is_reverse_path: bool = True,
    ) -> float:
        return self.calculate_roundtrip(
            0.0,
            trade_usdc_micro,
            vol_5m_pct=vol_5m_pct,
            wallet_sol=wallet_sol,
            cex_sol=cex_sol,
            is_reverse_path=is_reverse_path,
        ).total_cost_bps


def use_advanced_cost_model() -> bool:
    return _env_bool("V2_USE_ADVANCED_COST_MODEL", True)


_default_model: AdvancedCostModel | None = None


def get_advanced_cost_model() -> AdvancedCostModel:
    global _default_model
    if _default_model is None:
        _default_model = AdvancedCostModel()
    return _default_model


def refresh_advanced_cost_model(config: dict[str, Any] | None = None) -> AdvancedCostModel:
    global _default_model
    _default_model = AdvancedCostModel(config)
    return _default_model


def reset_advanced_cost_model() -> AdvancedCostModel:
    return refresh_advanced_cost_model()
