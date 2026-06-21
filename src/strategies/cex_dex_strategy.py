# src/strategies/cex_dex_strategy.py
"""
Production CEX-DEX arbitrage strategy — Backpack CEX + Jupiter DEX + Jito bundles.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.cex.backpack import BackpackClient
from src.cex.trading_pairs import CexDexPair, load_cex_dex_pairs
from src.config.settings import Settings, get_settings
from src.core.risk import RiskEngine
from src.core.sizing import calculate_trade_size as sizing_calculate_trade_size
from src.core.sizing import get_max_trade_size_micro
from src.core.wallet import get_onchain_usdc_balance
from src.dex.jupiter import SOL_MINT, USDC_MINT, JupiterClient
from src.dex.jupiter_params import quote_route_hops, resolve_slippage_bps
from src.execution.jito_bundle import JitoBundleExecutor
from src.core.jito_tip import (
    calculate_optimal_tip,
    get_cached_tip_floor,
    log_jito_tip,
    mev_protection_enabled,
    modeled_net_usd,
)
from src.core.multi_region_jito_sender import multi_region_enabled, send_bundle_multi_region
from src.monitoring.near_miss_log import append_cex_dex_near_miss
from src.monitoring.metrics import (
    record_cex_dex_near_miss,
    record_execution_slippage,
    record_probe_exec_decay,
    record_rescue_negative_event,
    record_roundtrip_divergence_event,
    record_trade_execution,
    record_trade_opportunity,
    record_trade_signal,
)
from src.monitoring.win_rate_tracker import (
    LIVE_MIN_WIN_RATE,
    WinRateTracker,
    get_win_rate_tracker,
)
from src.strategies.cex_dex_core import (
    analyze_cex_dex_spread,
    clamp_trade_usdc_micro,
    dynamic_min_trade_usdc_micro,
    gate_cex_dex_ask_depth,
    gate_cex_dex_direction,
    modeled_roundtrip_cost_bps,
    net_spread_bps_after_costs,
    resolve_direction,
    set_cex_cheap_flags,
)
from src.strategies.volatility_gate import VolatilityGate
from src.core.capital_preflight import (
    InsufficientBalance,
    get_ledger_sol_balance,
    preflight_check,
)
from src.strategies.cex_dex_roundtrip import (
    pre_simulate_cex_buy_dex_sell,
    pre_simulate_full_jupiter_roundtrip,
    roundtrip_sim_min_net_bps,
)
from src.strategies.roundtrip_sim import RoundtripSimulator
from src.utils.price import bps_diff

logger = logging.getLogger(__name__)

# High-liquidity midcaps only (override via ``CEX_PROVEN_MIDCAPS``; empty = SOL-only).
PROVEN_MIDCAPS: tuple[str, ...] = ()
_RESCUE_PAIR_POLICY_CACHE: dict[str, Any] = {}
_RESCUE_PAIR_POLICY_MTIME = 0.0
_RESCUE_PAIR_POLICY_LOADED_AT = 0.0
_RESCUE_CALIBRATION_CACHE: dict[str, Any] = {}
_RESCUE_CALIBRATION_MTIME = 0.0
_RESCUE_CALIBRATION_LOADED_AT = 0.0


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _rescue_pair_policy_path() -> Path:
    raw = (os.getenv("CEX_DEX_MODEL_NET_SOFT_RESCUE_PAIR_POLICY_PATH") or "").strip()
    return Path(raw or "logs/rescue_pair_policy.json")


def _load_rescue_pair_policy() -> dict[str, Any]:
    global _RESCUE_PAIR_POLICY_CACHE
    global _RESCUE_PAIR_POLICY_MTIME
    global _RESCUE_PAIR_POLICY_LOADED_AT
    path = _rescue_pair_policy_path()
    now = time.time()
    ttl = max(5.0, _env_float("CEX_DEX_MODEL_NET_SOFT_RESCUE_POLICY_CACHE_SEC", 60.0))
    if now - _RESCUE_PAIR_POLICY_LOADED_AT < ttl:
        return _RESCUE_PAIR_POLICY_CACHE
    _RESCUE_PAIR_POLICY_LOADED_AT = now
    if not path.is_file():
        _RESCUE_PAIR_POLICY_CACHE = {}
        _RESCUE_PAIR_POLICY_MTIME = 0.0
        return _RESCUE_PAIR_POLICY_CACHE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _RESCUE_PAIR_POLICY_CACHE
    if mtime != _RESCUE_PAIR_POLICY_MTIME:
        _RESCUE_PAIR_POLICY_CACHE = _read_json_file(path)
        _RESCUE_PAIR_POLICY_MTIME = mtime
    return _RESCUE_PAIR_POLICY_CACHE


def _rescue_calibration_path() -> Path:
    raw = (os.getenv("CEX_DEX_MODEL_NET_SOFT_RESCUE_CALIBRATION_PATH") or "").strip()
    return Path(raw or "logs/rescue_confidence_calibration.json")


def _load_rescue_calibration() -> dict[str, Any]:
    global _RESCUE_CALIBRATION_CACHE
    global _RESCUE_CALIBRATION_MTIME
    global _RESCUE_CALIBRATION_LOADED_AT
    path = _rescue_calibration_path()
    now = time.time()
    ttl = max(5.0, _env_float("CEX_DEX_MODEL_NET_SOFT_RESCUE_CALIBRATION_CACHE_SEC", 120.0))
    if now - _RESCUE_CALIBRATION_LOADED_AT < ttl:
        return _RESCUE_CALIBRATION_CACHE
    _RESCUE_CALIBRATION_LOADED_AT = now
    if not path.is_file():
        _RESCUE_CALIBRATION_CACHE = {}
        _RESCUE_CALIBRATION_MTIME = 0.0
        return _RESCUE_CALIBRATION_CACHE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _RESCUE_CALIBRATION_CACHE
    if mtime != _RESCUE_CALIBRATION_MTIME:
        _RESCUE_CALIBRATION_CACHE = _read_json_file(path)
        _RESCUE_CALIBRATION_MTIME = mtime
    return _RESCUE_CALIBRATION_CACHE


def _load_proven_midcaps() -> frozenset[str]:
    raw = (os.getenv("CEX_PROVEN_MIDCAPS") or "").strip()
    if raw:
        return frozenset(s.strip().upper() for s in raw.split(",") if s.strip())
    return frozenset(PROVEN_MIDCAPS)


def _soft_rescue_symbols() -> set[str]:
    raw = (
        os.getenv("CEX_DEX_MODEL_NET_SOFT_RESCUE_SYMBOLS")
        or "SOL,WIF,BONK,POPCAT,MEW,PNUT,FARTCOIN"
    ).strip()
    if not raw:
        return {"SOL", "WIF", "BONK", "POPCAT", "MEW", "PNUT", "FARTCOIN"}
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def _soft_rescue_min_sim_net_bps() -> float:
    # Keep rescue conservative by default (>= 0.5 bps simulated net) unless explicitly relaxed.
    return _env_float("CEX_DEX_MODEL_NET_SOFT_RESCUE_MIN_SIM_NET_BPS", 0.5)


def _soft_rescue_negative_sim_guardrails(symbol: str | None = None) -> dict[str, float | bool]:
    """Optional near-breakeven mode: allow small negative sim net under strict constraints."""
    min_edge = _env_float("CEX_DEX_MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_MIN_EDGE_BPS", 35.0)
    min_conf = _env_float("CEX_DEX_MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_MIN_AI_CONF", 78.0)
    max_size = _env_float("CEX_DEX_MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_MAX_SIZE_USDC", 12.0)
    max_loss = abs(_env_float("CEX_DEX_MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_MAX_LOSS_BPS", 5.0))
    out: dict[str, float | bool] = {
        "enabled": _env_bool("CEX_DEX_MODEL_NET_SOFT_RESCUE_ALLOW_NEGATIVE_SIM", False),
        "min_edge_bps": max(0.0, min_edge),
        "min_ai_conf": max(0.0, min_conf),
        "max_size_usdc": max(0.5, max_size),
        "max_loss_bps": max(0.0, max_loss),
    }
    base = (symbol or "").strip().upper()
    if not base:
        return out
    pairs = (_load_rescue_pair_policy().get("pairs") or {})
    pair_cfg = pairs.get(base)
    if not isinstance(pair_cfg, dict):
        return out
    if pair_cfg.get("enabled") is not None:
        out["enabled"] = bool(pair_cfg.get("enabled"))
    for key in ("min_edge_bps", "min_ai_conf", "max_size_usdc", "max_loss_bps"):
        if key not in pair_cfg:
            continue
        try:
            out[key] = float(pair_cfg[key])
        except (TypeError, ValueError):
            continue
    return out


def _rescue_recalibrated_confidence(confidence: float, symbol: str, sim_net_bps: float) -> float:
    if not _env_bool("CEX_DEX_MODEL_NET_SOFT_RESCUE_RECALIBRATE_CONFIDENCE", True):
        return confidence
    cfg = _load_rescue_calibration()
    pairs = cfg.get("pairs") if isinstance(cfg.get("pairs"), dict) else {}
    pair_cfg = pairs.get((symbol or "").strip().upper()) if isinstance(pairs, dict) else None
    default_cfg = cfg.get("default") if isinstance(cfg.get("default"), dict) else {}
    use_cfg = pair_cfg if isinstance(pair_cfg, dict) else default_cfg
    if not isinstance(use_cfg, dict):
        return confidence
    try:
        bias = float(use_cfg.get("bias", 0.0))
    except (TypeError, ValueError):
        bias = 0.0
    try:
        sim_slope = float(use_cfg.get("sim_net_slope", 0.0))
    except (TypeError, ValueError):
        sim_slope = 0.0
    adjusted = float(confidence) + bias + (sim_slope * float(sim_net_bps))
    return max(0.0, min(100.0, adjusted))


def _soft_rescue_size_ladder() -> list[float]:
    """Rescue-only size multipliers (largest to smallest) to recover near-breakeven signals."""
    raw = (
        os.getenv("CEX_DEX_MODEL_NET_SOFT_RESCUE_SIZE_LADDER")
        or "1.0,0.7,0.5,0.35,0.25,0.18,0.12"
    ).strip()
    multipliers: list[float] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            mult = float(token)
        except ValueError:
            continue
        if mult > 0:
            multipliers.append(mult)
    if not multipliers:
        multipliers = [1.0, 0.7, 0.5, 0.35, 0.25, 0.18, 0.12]
    return multipliers


def evaluate_cex_dex_opportunity(
    cex_mid: float,
    jup_price: float,
    size_usdc: int,
    *,
    settings: Settings | None = None,
    direction: str | None = None,
) -> bool:
    """Gross-spread gate with optional near-miss logging (used by ``cex_dex_cycle``)."""
    cfg = settings or get_settings()
    if cex_mid <= 0 or jup_price <= 0:
        return False

    spread = analyze_cex_dex_spread(cex_mid, jup_price)
    if spread is None:
        return False

    trade_dir = resolve_direction(direction, cex_mid, jup_price) or spread.direction
    edge_bps = float(bps_diff(cex_mid, jup_price))
    gross_bps = edge_bps if trade_dir == "cex_cheap" else spread.spread_bps_abs
    net_bps = net_spread_bps_after_costs(
        abs(edge_bps),
        size_usdc,
        direction=trade_dir,
    )
    min_gross = cfg.CEX_DEX_MIN_GROSS_SPREAD_BPS

    if gross_bps >= min_gross:
        logger.info(
            "CEX-DEX SIGNAL | gross=%.1f model_net=%.1f dir=%s size=%d",
            gross_bps,
            net_bps,
            trade_dir,
            size_usdc,
        )
        record_trade_opportunity("cex_dex", int(gross_bps), int(net_bps))
        return True

    if cfg.CEX_DEX_LOG_NEAR_MISSES:
        logger.info(
            "CEX-DEX NEAR_MISS | gross=%.1f (need %d) model_net=%.1f",
            gross_bps,
            min_gross,
            net_bps,
        )
        record_cex_dex_near_miss(gross_bps)
    return False


class CexDexStrategy:
    """
    Next-level CEX-DEX strategy: volatility adaptive gates, inventory-first execution,
    full CEX→withdraw→DEX fallback, direction filtering, enhanced trade logging.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        risk_engine: RiskEngine | None = None,
        win_rate_tracker: WinRateTracker | None = None,
        *,
        backpack_client: BackpackClient | None = None,
        jupiter_executor: JupiterClient | None = None,
        wallet_pubkey: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.risk = risk_engine or RiskEngine(self.settings)
        self.win_rate_tracker = win_rate_tracker or get_win_rate_tracker()
        self.backpack = backpack_client or BackpackClient(self.settings)
        self.jupiter = jupiter_executor or JupiterClient(self.settings)
        self.jito = JitoBundleExecutor(self.settings)
        self.wallet_pubkey = (
            wallet_pubkey
            or self.settings.wallet_pubkey
            or getattr(self.settings, "WALLET_PUBKEY", None)
            or os.getenv("WALLET_PUBKEY", "")
        )
        self.vol_gate = VolatilityGate(self.backpack, self.jupiter)
        from src.strategies.dex_cex_reverse import DexCexReverseStrategy

        self.reverse_strategy = DexCexReverseStrategy(
            jupiter_executor=self.jupiter,
            backpack_client=self.backpack,
            wallet_pubkey=self.wallet_pubkey,
            settings=self.settings,
            risk=self.risk,
        )
        self.trade_history_path = Path(
            os.getenv("TRADE_HISTORY_PATH", "logs/trade_history.jsonl")
        )
        self.trade_history_path.parent.mkdir(parents=True, exist_ok=True)
        self.base_cost_bps = float(self.settings.CEX_DEX_STRATEGY_BASE_COST_BPS)
        self._proven_midcaps = _load_proven_midcaps()
        self._pairs = self._load_tradeable_pairs()
        self._scan_priority_symbol: str | None = None
        self._pair_roundtrip_fail_state: dict[str, dict[str, float | int]] = {}
        self._pair_quality_state: dict[str, dict[str, float | int]] = {}
        self._runtime_exec_quality: list[dict[str, float | bool]] = []
        self._pair_route_pref: dict[str, dict[str, float]] = {}
        logger.info(
            "CEX-DEX pairs | count=%d proven_midcaps=%s symbols=%s",
            len(self._pairs),
            ",".join(sorted(self._proven_midcaps)),
            ",".join(p.symbol for p in self._pairs),
        )

    @property
    def jupiter_executor(self) -> JupiterClient:
        """Alias for ``self.jupiter`` (inventory / legacy call sites)."""
        return self.jupiter

    @property
    def backpack_client(self) -> BackpackClient:
        """Alias for ``self.backpack`` (vol / opportunistic helpers)."""
        return self.backpack

    def _env_bool(self, name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    def _volatility_gate(self) -> VolatilityGate:
        return self.vol_gate

    async def should_use_opportunistic_mode(self) -> bool:
        """True when adaptive tier is opportunistic or aggressive."""
        if not self._env_bool("CEX_DEX_VOL_OPPORTUNISTIC_AUTO", True):
            return False
        try:
            gates = await self._volatility_gate().get_adaptive_gates()
            mode = str(gates.get("mode") or "strict")
            return mode in ("opportunistic", "aggressive")
        except Exception as exc:
            logger.debug("should_use_opportunistic_mode failed: %s", exc)
            return False

    async def get_dynamic_gates(self) -> dict[str, float | str]:
        """Adaptive detection gates from ``VolatilityGate`` (strict / opportunistic / aggressive)."""
        if not _env_bool("CEX_DEX_ADAPTIVE_GATES_ENABLED", True):
            return {
                "min_gross": float(self.settings.CEX_DEX_MIN_GROSS_SPREAD_BPS),
                "min_net": float(self.settings.CEX_DEX_MIN_NET_SPREAD_BPS),
                "ai_conf": float(self.settings.AI_APPROVE_MIN_CONFIDENCE),
                "roundtrip_min": float(
                    getattr(self.settings, "CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS", 3)
                ),
                "mode": "env",
            }
        return await self._volatility_gate().get_adaptive_gates()

    def _load_tradeable_pairs(self) -> list[CexDexPair]:
        """SOL + proven midcaps from env (``CEX_MIDCAPS`` / ``CEX_MAX_MIDCAPS``)."""
        loaded = load_cex_dex_pairs()
        out: list[CexDexPair] = []
        for pair in loaded:
            base = pair.symbol.upper()
            if base == "SOL":
                out.append(pair)
            elif base in self._proven_midcaps:
                out.append(pair)
        return out

    def _is_thin_book(self, depth: dict[str, Any]) -> bool:
        """True when ask book cannot support probe-sized CEX buy or spread is too wide."""
        from src.cex.backpack_ticker import best_bid_ask_from_book, cumulative_ask_usdc

        if not depth or not depth.get("asks"):
            return True

        spread_cap = float(os.getenv("CEX_MAX_BOOK_SPREAD_BPS", "50"))
        top = best_bid_ask_from_book(depth)
        if top is not None:
            best_bid, best_ask = top
            if best_bid > 0 and best_ask > 0 and best_ask >= best_bid:
                spread_bps = (best_ask - best_bid) / best_ask * 10_000.0
                if spread_bps > spread_cap:
                    return True

        mult = float(os.getenv("CEX_MIN_BOOK_DEPTH_MULT", "1.5"))
        required_usdc = (self._probe_usdc_micro() / 1_000_000.0) * mult
        return cumulative_ask_usdc(depth, max_levels=20) < required_usdc

    async def _rescue_depth_size_hints(
        self,
        pair: CexDexPair,
        *,
        start_size_micro: int,
        rescue_min_trade_micro: int,
        max_trade_micro: int,
    ) -> list[int]:
        """Depth-derived candidates to avoid static ladder blind spots."""
        if not _env_bool("CEX_DEX_MODEL_NET_SOFT_RESCUE_USE_DEPTH_HINTS", True):
            return []
        depth_levels = max(3, _env_int("CEX_DEX_MODEL_NET_SOFT_RESCUE_DEPTH_LEVELS", 12))
        depth = await self.backpack.get_depth(pair.backpack_symbol, limit=depth_levels)
        asks = depth.get("asks") if isinstance(depth, dict) else None
        if not isinstance(asks, list) or not asks:
            return []
        cum_usdc = 0.0
        levels_usdc: list[float] = []
        for ask in asks:
            if not isinstance(ask, (list, tuple)) or len(ask) < 2:
                continue
            try:
                px = float(ask[0])
                qty = float(ask[1])
            except (TypeError, ValueError):
                continue
            if px <= 0 or qty <= 0:
                continue
            cum_usdc += px * qty
            levels_usdc.append(cum_usdc)
        if not levels_usdc:
            return []
        fr_raw = (
            os.getenv("CEX_DEX_MODEL_NET_SOFT_RESCUE_DEPTH_FRACTIONS")
            or "0.25,0.4,0.6,0.8,1.0"
        )
        fractions: list[float] = []
        for token in fr_raw.split(","):
            tok = token.strip()
            if not tok:
                continue
            try:
                value = float(tok)
            except ValueError:
                continue
            if 0 < value <= 1.0:
                fractions.append(value)
        if not fractions:
            fractions = [0.25, 0.4, 0.6, 0.8, 1.0]
        max_depth_usdc = min(levels_usdc[-1], start_size_micro / 1_000_000.0)
        candidates: list[int] = []
        for frac in fractions:
            usdc = max_depth_usdc * frac
            micro = int(usdc * 1_000_000.0)
            clamped = max(rescue_min_trade_micro, min(max_trade_micro, micro))
            if clamped not in candidates:
                candidates.append(clamped)
        return candidates

    def _negative_tier_state_path(self) -> Path:
        raw = (os.getenv("CEX_DEX_MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_STATE_PATH") or "").strip()
        return Path(raw or "logs/rescue_negative_tier_state.json")

    def _negative_tier_budget_allows(self) -> tuple[bool, str]:
        if not _env_bool("CEX_DEX_MODEL_NET_SOFT_RESCUE_ALLOW_NEGATIVE_SIM", False):
            return False, "disabled"
        daily_budget = max(1, _env_int("CEX_DEX_MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_DAILY_BUDGET", 6))
        max_consec_losses = max(
            1,
            _env_int("CEX_DEX_MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_MAX_CONSEC_LOSSES", 2),
        )
        path = self._negative_tier_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).date().isoformat()
        state = {
            "day": today,
            "attempts": 0,
            "consecutive_losses": 0,
        }
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    state.update(data)
            except Exception:
                pass
        if str(state.get("day")) != today:
            state = {"day": today, "attempts": 0, "consecutive_losses": 0}
            path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        attempts = int(state.get("attempts") or 0)
        consec = int(state.get("consecutive_losses") or 0)
        if attempts >= daily_budget:
            return False, "daily_budget"
        if consec >= max_consec_losses:
            return False, "consecutive_losses"
        return True, "ok"

    def _negative_tier_record_attempt(self) -> None:
        path = self._negative_tier_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).date().isoformat()
        state = {"day": today, "attempts": 0, "consecutive_losses": 0}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    state.update(data)
            except Exception:
                pass
        if str(state.get("day")) != today:
            state = {"day": today, "attempts": 0, "consecutive_losses": 0}
        state["attempts"] = int(state.get("attempts") or 0) + 1
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _negative_tier_record_outcome(self, success: bool) -> None:
        path = self._negative_tier_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).date().isoformat()
        state = {"day": today, "attempts": 0, "consecutive_losses": 0}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    state.update(data)
            except Exception:
                pass
        if str(state.get("day")) != today:
            state = {"day": today, "attempts": 0, "consecutive_losses": 0}
        if success:
            state["consecutive_losses"] = 0
        else:
            state["consecutive_losses"] = int(state.get("consecutive_losses") or 0) + 1
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    async def should_trade_pair(self, pair: CexDexPair | str) -> bool:
        """SOL always; midcaps only if proven and book depth is sufficient."""
        if isinstance(pair, CexDexPair):
            label = pair.pair_label
            base = pair.symbol.upper()
            backpack_symbol = pair.backpack_symbol
        else:
            label = str(pair)
            base = label.split("/")[0].strip().upper()
            backpack_symbol = f"{base}_USDC"

        if label == "SOL/USDC" or base == "SOL":
            return True

        if base not in self._proven_midcaps:
            return False

        priority = getattr(self, "_scan_priority_symbol", None)
        if priority and base.upper() == str(priority).strip().upper():
            return True

        depth = await self.backpack.get_depth(backpack_symbol, limit=20)
        if not depth or self._is_thin_book(depth):
            logger.info("Thin book skipped: %s", label)
            return False
        return True

    def _estimated_cost_bps(self, size_usdc_micro: int) -> float:
        """Modeled round-trip drag (bps) for this trade size."""
        return float(
            self.settings.trading.cex_dex_strategy_base_cost_bps
            or self.settings.CEX_DEX_STRATEGY_BASE_COST_BPS
            or self.base_cost_bps
        )

    def _probe_usdc_micro(self) -> int:
        return int(
            os.getenv(
                "CEX_DEX_PROBE_USDC_MICRO",
                str(
                    getattr(
                        self.settings,
                        "CEX_DEX_PROBE_USDC_MICRO",
                        int(self.settings.trading.min_flash_usdc * 1_000_000 // 2),
                    )
                ),
            )
        )

    def _is_profitable_opportunity(
        self,
        edge_bps: float,
        net_bps: float,
        ai_conf: float,
        *,
        gates: dict[str, float] | None = None,
    ) -> bool:
        """Directional edge for CEX-buy → DEX-sell (positive edge = Jupiter richer than CEX)."""
        if self.settings.CEX_DEX_AGGRESSIVE_OPPORTUNITY_FILTER:
            effective_cost = self.base_cost_bps * (0.75 if edge_bps > 18 else 1.0)
            economic_net = edge_bps - effective_cost
            return (
                economic_net >= 3.0
                and edge_bps >= 6.0
                and ai_conf >= 55
            )

        g = gates or {}
        min_gross = float(g.get("min_gross", self.settings.CEX_DEX_MIN_GROSS_SPREAD_BPS))
        min_net = float(g.get("min_net", self.settings.CEX_DEX_MIN_NET_SPREAD_BPS))
        min_ai = float(g.get("ai_conf", self.settings.AI_APPROVE_MIN_CONFIDENCE))
        return edge_bps >= min_gross and net_bps >= min_net and ai_conf >= min_ai

    def should_log_near_miss(
        self,
        opportunity: dict[str, Any],
        *,
        gates: dict[str, float | str] | None = None,
    ) -> bool:
        """Log near-miss with actionable reason tags when opportunity fails gate checks."""
        if not getattr(self.settings, "CEX_DEX_LOG_NEAR_MISSES", True):
            return False

        g = gates or {}
        min_gross = float(
            g.get("min_gross", self.settings.CEX_DEX_MIN_GROSS_SPREAD_BPS)
        )
        min_net = float(g.get("min_net", self.settings.CEX_DEX_MIN_NET_SPREAD_BPS))
        ai_floor = float(
            os.getenv(
                "CEX_DEX_AI_CONFIDENCE_FLOOR",
                str(getattr(self.settings, "CEX_DEX_AI_CONFIDENCE_FLOOR", 62)),
            )
        )

        gross = float(
            opportunity.get("gross_bps")
            or opportunity.get("edge_bps")
            or 0
        )
        net = float(opportunity.get("net_bps") or 0)
        ai_conf = float(
            opportunity.get("ai_confidence")
            or opportunity.get("ai_conf")
            or 0
        )
        direction = str(opportunity.get("direction") or "")
        size_micro = int(
            opportunity.get("size_usdc_micro")
            or opportunity.get("size_usdc")
            or 0
        )
        pair = str(
            opportunity.get("pair_label")
            or opportunity.get("pair")
            or "SOL/USDC"
        )

        reasons: list[str] = []
        if gross < min_gross * 0.7:
            reasons.append("gross_too_low")
        elif net < min_net:
            reasons.append(f"net_below_{min_net:g}")
        elif ai_conf < ai_floor:
            reasons.append("ai_confidence_low")
        elif direction == "dex_cheap":
            reasons.append("wrong_direction_dex_cheap")

        if not reasons:
            return False

        reason = "|".join(reasons)
        from src.strategies.near_miss_gate import should_emit_near_miss

        if not should_emit_near_miss(
            reason,
            gross_bps=gross,
            net_bps=net,
            min_net_bps=min_net,
            direction=direction,
        ):
            return False

        size_usdc = size_micro / 1_000_000.0
        logger.info(
            "NEAR_MISS | pair=%s gross=%.1f net=%.1f ai=%.1f reason=%s size_usdc=%.2f",
            pair,
            gross,
            net,
            ai_conf,
            reason,
            size_usdc,
        )
        record_cex_dex_near_miss(gross, reason=reason)
        append_cex_dex_near_miss(
            {
                "pair": pair,
                "gross_bps": gross,
                "net_bps": net,
                "ai_conf": ai_conf,
                "reason": reason,
            }
        )
        return True

    def log_near_miss(
        self,
        gross_bps: float,
        net_bps: float,
        ai_conf: float,
        reason: str,
        *,
        pair: str = "SOL/USDC",
    ) -> None:
        """Near-miss logging (explicit reason); see ``should_log_near_miss`` for auto-tagging."""
        from src.strategies.near_miss_gate import should_emit_near_miss

        min_net = float(self.settings.CEX_DEX_MIN_NET_SPREAD_BPS)
        if not should_emit_near_miss(
            reason,
            gross_bps=gross_bps,
            net_bps=net_bps,
            min_net_bps=min_net,
        ):
            return
        if getattr(self.settings, "CEX_DEX_LOG_NEAR_MISSES", True):
            logger.info(
                "NEAR_MISS | pair=%s gross=%.1f net=%.1f ai=%.1f%% reason=%s",
                pair,
                gross_bps,
                net_bps,
                ai_conf,
                reason,
            )
            record_cex_dex_near_miss(gross_bps, reason=reason)
            append_cex_dex_near_miss(
                {
                    "pair": pair,
                    "gross_bps": gross_bps,
                    "net_bps": net_bps,
                    "ai_conf": ai_conf,
                    "reason": reason,
                }
            )

    async def get_5m_volatility(self, symbol: str = "SOL_USDC") -> float:
        """5-minute CEX volatility % (return stdev or range fallback)."""
        _ = symbol
        return await self._volatility_gate().get_5min_volatility()

    async def _probe_jupiter_sell_price(
        self,
        pair: CexDexPair,
        cex_buy: float,
    ) -> float | None:
        """DEX sell-leg USDC/base for spread probes (aligned with roundtrip sim)."""
        probe_micro = self._probe_usdc_micro()
        sell_px, _ = await self.jupiter.get_implied_usdc_per_base_sell(
            probe_micro,
            pair.base_mint,
            float(cex_buy),
            base_decimals=pair.base_decimals,
        )
        jup_price = float(sell_px) if sell_px and sell_px > 0 else None
        if jup_price is None:
            jup_price, _ = await self.jupiter.get_implied_usdc_per_base(
                probe_micro,
                pair.base_mint,
                base_decimals=pair.base_decimals,
            )
            jup_price = float(jup_price) if jup_price else None
        if pair.symbol == "SOL" and jup_price and jup_price > 0:
            from src.dex.executor import get_dex_executor

            try:
                dex_q = await get_dex_executor().get_best_dex_price(
                    probe_micro,
                    use_phoenix=True,
                    jupiter_price=jup_price,
                )
                if dex_q and dex_q.price > 0:
                    jup_price = float(dex_q.price)
            except Exception as exc:
                logger.debug("Phoenix-enhanced probe skipped: %s", exc)
        return jup_price

    async def _jupiter_price_for_cex_buy(
        self,
        pair: CexDexPair,
        size_micro: int,
        cex_buy: float,
    ) -> float | None:
        """Implied DEX USDC/base for a CEX buy leg at ``size_micro`` (sell quote preferred)."""
        sell_px, _ = await self.jupiter.get_implied_usdc_per_base_sell(
            size_micro,
            pair.base_mint,
            float(cex_buy),
            base_decimals=pair.base_decimals,
        )
        if sell_px and sell_px > 0:
            return float(sell_px)
        slippage = resolve_slippage_bps(USDC_MINT, pair.base_mint)
        px, _ = await self.jupiter.get_implied_usdc_per_base(
            size_micro,
            pair.base_mint,
            base_decimals=pair.base_decimals,
            slippage_bps=slippage,
        )
        return float(px) if px and px > 0 else None

    async def _quick_gross_bps(self, pair: CexDexPair | None = None) -> float:
        """Lightweight gross spread probe for vol gate (one pair)."""
        target = pair or (self._pairs[0] if self._pairs else None)
        if target is None:
            return 0.0
        cex_buy, _, _ = await self.backpack.get_cex_buy_reference_price(target.backpack_symbol)
        if not cex_buy or cex_buy <= 0:
            return 0.0
        jup_price = await self._probe_jupiter_sell_price(target, float(cex_buy))
        if not jup_price or jup_price <= 0:
            return 0.0
        from src.utils.price import bps_diff

        return abs(float(bps_diff(cex_buy, jup_price)))

    def _focus_scan_symbols(self) -> set[str] | None:
        """Liquid pairs for detect loop; ``all`` / empty disables focus filter."""
        raw = os.getenv(
            "CEX_DEX_FOCUS_SCAN_SYMBOLS",
            "SOL,BONK,WIF,POPCAT,MEW,PNUT,FARTCOIN",
        ).strip()
        if raw.lower() in ("", "all", "*", "none", "false", "0"):
            return None
        return {s.strip().upper() for s in raw.split(",") if s.strip()}

    def _pairs_for_scan(self, priority_symbol: str | None = None) -> list[CexDexPair]:
        """Scan order: vol-gate best pair first, optional liquid-only filter."""
        focus = self._focus_scan_symbols()
        pairs = list(self._pairs)
        if focus is not None:
            pairs = [p for p in pairs if p.symbol.upper() in focus]
        if priority_symbol:
            pri = priority_symbol.strip().upper()
            pairs.sort(key=lambda p: 0 if p.symbol.upper() == pri else 1)
        return pairs

    async def _rank_pairs_by_probe(self) -> list[tuple[CexDexPair, float]]:
        """Rank scan pairs by fresh probe gross edge (descending)."""
        ranked: list[tuple[CexDexPair, float]] = []
        for pair in self._pairs_for_scan():
            try:
                gross = await self._quick_gross_bps(pair)
            except Exception as exc:
                logger.debug("Vol gate probe skipped for %s: %s", pair.symbol, exc)
                gross = 0.0
            ranked.append((pair, gross))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked

    async def _max_gross_across_pairs(self) -> tuple[float, str]:
        """Max gross bps across configured pairs (vol gate uses best edge, not SOL-only)."""
        max_gross = 0.0
        best_pair = self._pairs[0].symbol if self._pairs else "SOL"

        for pair in self._pairs:
            try:
                gross = await self._quick_gross_bps(pair)
            except Exception as exc:
                logger.debug("Vol gate probe skipped for %s: %s", pair.symbol, exc)
                continue
            if gross > max_gross:
                max_gross = gross
                best_pair = pair.symbol

        return max_gross, best_pair

    async def run_cycle(self) -> bool:
        """Main strategy cycle — CEX-DEX scan, smart path execute, then reverse fallback."""
        if not self.risk.can_trade(0):
            return False

        from src.strategies.volatility_gate import should_skip_low_vol_cycle

        gates = await self.vol_gate.get_adaptive_gates()
        vol_5m = await self.vol_gate.get_5min_volatility()
        ranked_pairs = await self._rank_pairs_by_probe()
        max_gross = ranked_pairs[0][1] if ranked_pairs else 0.0
        best_pair = ranked_pairs[0][0].symbol if ranked_pairs else "SOL"
        if should_skip_low_vol_cycle(vol_5m, max_gross, best_pair=best_pair):
            return False

        scan_pairs = [pair for pair, _ in ranked_pairs]
        top_n = _env_int("CEX_DEX_EXEC_SCAN_TOP_N", 4)
        if top_n > 0:
            scan_pairs = scan_pairs[:top_n]

        logger.info(
            "Vol gate pass | vol_5m=%.3f%% max_gross=%.2f bps best_pair=%s",
            float(vol_5m or 0.0),
            max_gross,
            best_pair,
        )

        logger.info(
            "Starting multi-pair scan | pairs=%d liquid_scan=%d exec_scan=%d",
            len(self._pairs),
            len(self._pairs_for_scan(best_pair)),
            len(scan_pairs),
        )
        logger.info(
            "Adaptive gates: %s | vol=%.2f%%",
            gates.get("mode"),
            vol_5m,
        )

        self._scan_priority_symbol = best_pair
        opportunity: dict[str, Any] | None = None
        try:
            opportunity = await self._scan_cex_dex_opportunity(
                gates,
                priority_symbol=best_pair,
                scan_pairs=scan_pairs,
            )
        except Exception as scan_exc:
            logger.error(
                "CEX-DEX scan error | %s",
                scan_exc,
                exc_info=True,
            )
        finally:
            self._scan_priority_symbol = None
        if opportunity:
            execution_queue: list[dict[str, Any]] = [opportunity]
            for queued in opportunity.get("_candidate_queue") or []:
                if not isinstance(queued, dict):
                    continue
                prepared = await self._scan_cex_dex_opportunity(
                    gates,
                    candidate_override=queued,
                )
                if prepared is None:
                    continue
                execution_queue.append(prepared)

            max_exec = max(1, _env_int("CEX_DEX_EXEC_CANDIDATE_EXEC_N", 3))
            for idx, candidate in enumerate(execution_queue[:max_exec], start=1):
                logger.info(
                    "EXEC_CANDIDATE_TRY | idx=%d/%d pair=%s rank=%.1fbps",
                    idx,
                    min(len(execution_queue), max_exec),
                    str(candidate.get("pair_label") or "SOL/USDC"),
                    float(candidate.get("rank_bps") or candidate.get("net_bps") or 0.0),
                )
                result = await self._execute_smart_path(candidate)
                logged = await self._log_trade(result)
                if bool(
                    logged.get("live_fill")
                    or logged.get("success")
                    or result.get("success")
                ):
                    return True
            return False

        reverse_result = await self.reverse_strategy.scan_and_execute()
        if reverse_result.get("live_fill"):
            await self._log_trade(
                {
                    **reverse_result,
                    "strategy": "dex_cex_reverse",
                    "path": reverse_result.get("path", "dex_cex_reverse"),
                }
            )
            return True
        return False

    async def _scan_cex_dex_opportunity(
        self,
        gates: dict[str, Any],
        *,
        priority_symbol: str | None = None,
        scan_pairs: list[CexDexPair] | None = None,
        candidate_override: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Scan all configured pairs; return best execution-ready opportunity."""
        if candidate_override is not None:
            opp = dict(candidate_override)
        else:
            opp = await self.detect_opportunity(
                gates,
                priority_symbol=priority_symbol,
                scan_pairs=scan_pairs,
            )
        if opp is None:
            return None

        return await self._prepare_execution_candidate(opp, gates=gates)

    async def _prepare_execution_candidate(
        self,
        opp: dict[str, Any],
        *,
        gates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Normalize one detected opportunity into an execution-ready candidate."""

        ai_conf = float(opp.get("confidence") or 0.0)
        min_ai = float(
            gates.get("ai")
            or gates.get("ai_conf")
            or self.settings.AI_APPROVE_MIN_CONFIDENCE
        )
        if ai_conf < min_ai:
            self.log_near_miss(
                float(opp.get("gross_bps") or opp.get("edge_bps") or 0),
                float(opp.get("net_bps") or 0),
                ai_conf,
                "ai_conf_below_gate",
                pair=str(opp.get("pair_label") or ""),
            )
            return None

        lifecycle = self._pair_lifecycle_profile(str(opp.get("pair_label") or "SOL/USDC"))
        if not bool(lifecycle.get("allow_live", True)):
            self.log_near_miss(
                float(opp.get("gross_bps") or opp.get("edge_bps") or 0),
                float(opp.get("net_bps") or 0),
                ai_conf,
                "lifecycle_paper",
                pair=str(opp.get("pair_label") or ""),
            )
            return None

        extra_net = float(lifecycle.get("extra_net_bps") or 0.0)
        min_net = float(gates.get("min_net") or self.settings.CEX_DEX_MIN_NET_SPREAD_BPS)
        if float(opp.get("net_bps") or 0.0) < (min_net + extra_net):
            self.log_near_miss(
                float(opp.get("gross_bps") or opp.get("edge_bps") or 0),
                float(opp.get("net_bps") or 0),
                ai_conf,
                "lifecycle_net_gate",
                pair=str(opp.get("pair_label") or ""),
            )
            return None

        max_size_usdc = float(lifecycle.get("max_size_usdc") or 0.0)
        if max_size_usdc > 0:
            cap_micro = int(max_size_usdc * 1_000_000.0)
            cur_micro = int(opp.get("size_usdc") or opp.get("size_usdc_micro") or 0)
            if cur_micro > cap_micro > 0:
                opp["size_usdc"] = cap_micro
                opp["size_usdc_micro"] = cap_micro
                logger.info(
                    "PAIR_LIFECYCLE_SIZE_CAP | pair=%s mode=%s from=%.2f to=%.2f",
                    str(opp.get("pair_label") or "SOL/USDC"),
                    str(lifecycle.get("mode") or "full"),
                    cur_micro / 1_000_000.0,
                    cap_micro / 1_000_000.0,
                )

        opp["lifecycle_mode"] = str(lifecycle.get("mode") or "full")

        opp["type"] = "cex_dex"
        opp["ai_conf"] = ai_conf
        opp["path"] = "smart"
        if not opp.get("cex_ask"):
            backpack_symbol = str(opp.get("backpack_symbol") or "SOL_USDC")
            _, _, cex_ask = await self.backpack.get_cex_buy_reference_price(backpack_symbol)
            opp["cex_ask"] = cex_ask
        return opp

    async def _execute_smart_path(self, opp: dict[str, Any]) -> dict[str, Any]:
        """Inventory-first execution with full CEX→withdraw→DEX fallback (via execute_trade)."""
        size_micro = int(opp.get("size_usdc") or opp.get("size_usdc_micro") or 0)
        cex_px = float(opp.get("cex_price") or 0.0)
        wallet_sol = await self._get_wallet_sol_balance()
        required_lamports = int(
            opp.get("size_lamports")
            or self._estimate_sell_lamports(
                size_micro,
                cex_px,
                base_decimals=int(opp.get("base_decimals") or 9),
            )
        )
        buffer = float(os.getenv("CEX_DEX_INVENTORY_BUFFER_FRAC", "1.05"))
        have_lamports = int(wallet_sol * 1_000_000_000)
        if have_lamports >= int(required_lamports * buffer):
            logger.info("INVENTORY FAST PATH | sol=%.4f need_lamports=%s", wallet_sol, required_lamports)
        else:
            logger.info("FULL CEX -> WITHDRAW -> DEX PATH | sol=%.4f need_lamports=%s", wallet_sol, required_lamports)

        success = await self.execute_trade(opp)
        block_reason = str(opp.get("_block_reason") or "")
        return {
            "live_fill": success
            and not self.settings.test_mode
            and not self.settings.simulate,
            "success": success,
            "path": opp.get("_execution_path", "smart"),
            "gross_bps": float(opp.get("gross_bps") or opp.get("edge_bps") or 0),
            "net_bps": float(opp.get("net_bps") or 0),
            "status": "ok" if success else (block_reason or "blocked"),
            "block_reason": block_reason or None,
            "pair_label": str(opp.get("pair_label") or "SOL/USDC"),
            "strategy": "cex_dex",
            "tx_sig": opp.get("tx_sig"),
            "size_usdc_micro": size_micro,
        }

    async def _get_wallet_sol_balance(self) -> float:
        """On-chain SOL balance for configured wallet."""
        return await self.get_wallet_sol_balance()

    async def _log_trade(self, result: dict[str, Any]) -> dict[str, Any]:
        """Append structured trade entry to ``logs/trade_history.jsonl``."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "strategy": result.get("strategy", "cex_dex"),
            **result,
            "source": "live" if result.get("live_fill") else "live_blocked",
        }
        try:
            with self.trade_history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            logger.debug("trade_history write skipped: %s", exc)

        if result.get("live_fill"):
            logger.info(
                "LIVE FILL | path=%s gross=%.1fbps",
                result.get("path"),
                float(result.get("gross_bps") or 0),
            )
        else:
            logger.info("BLOCKED | reason=%s", result.get("status"))
        return entry

    async def detect_opportunity(
        self,
        gates: dict[str, Any] | None = None,
        *,
        priority_symbol: str | None = None,
        scan_pairs: list[CexDexPair] | None = None,
    ) -> dict[str, Any] | None:
        """Scan all configured pairs; return best ``cex_cheap`` opportunity by net bps."""
        gates = gates or await self.get_dynamic_gates()
        mode = str(gates.get("mode") or "strict")
        if mode in ("opportunistic", "aggressive", "strict"):
            logger.info(
                "ADAPTIVE_GATES | mode=%s vol_5m=%.3f%% gross>=%.0f net>=%.0f ai>=%.0f roundtrip>=%.0f",
                mode,
                float(gates.get("vol_5m") or 0),
                float(gates.get("min_gross", 0)),
                float(gates.get("min_net", 0)),
                float(gates.get("ai_conf", 0)),
                float(gates.get("roundtrip_min", 0)),
            )

        scan_pairs = list(scan_pairs) if scan_pairs is not None else self._pairs_for_scan(priority_symbol)
        focus = self._focus_scan_symbols()
        if priority_symbol:
            logger.info(
                "Scan order | priority=%s focus=%s pairs=%d",
                priority_symbol,
                ",".join(sorted(focus)) if focus else "all",
                len(scan_pairs),
            )

        best: dict[str, Any] | None = None
        parallel = self._env_bool("CEX_DEX_PARALLEL_SCAN", True)

        async def _scan_pair(pair: CexDexPair) -> dict[str, Any] | None:
            if not await self.should_trade_pair(pair):
                return None
            try:
                return await self._detect_pair_opportunity(pair, gates=gates)
            except Exception as exc:
                logger.debug("CEX-DEX scan %s failed: %s", pair.symbol, exc)
                return None

        if parallel and len(scan_pairs) > 1:
            results = await asyncio.gather(*[_scan_pair(p) for p in scan_pairs])
            candidates = [r for r in results if r is not None]
        else:
            candidates = []
            for pair in scan_pairs:
                opp = await _scan_pair(pair)
                if opp is not None:
                    candidates.append(opp)

        best_rank = float("-inf")
        score_weight_bps = _env_float("CEX_DEX_PAIR_SCORE_WEIGHT_BPS", 6.0)
        ranked_candidates: list[tuple[float, dict[str, Any]]] = []
        for opp in candidates:
            pair_label = str(opp.get("pair_label") or "SOL/USDC")
            fill_score = self._pair_fill_score(pair_label)
            bonus_bps = (fill_score - 0.5) * score_weight_bps
            route_bonus_bps = self._pair_route_pref_bonus_bps(
                pair_label,
                int(opp.get("detect_route_hops") or 0),
            )
            opp["pair_fill_score"] = fill_score
            opp["pair_fill_bonus_bps"] = bonus_bps
            opp["pair_route_bonus_bps"] = route_bonus_bps
            rank_bps = float(opp["net_bps"]) + bonus_bps + route_bonus_bps
            ranked_candidates.append((rank_bps, opp))
            if best is None or rank_bps > best_rank:
                best = opp
                best_rank = rank_bps
        ranked_candidates.sort(key=lambda item: item[0], reverse=True)
        log_top_n = max(1, _env_int("CEX_DEX_OPPORTUNITY_LOG_TOP_N", 3))
        for idx, (rank_bps, opp) in enumerate(ranked_candidates[:log_top_n], start=1):
            logger.info(
                "OPPORTUNITY_CANDIDATE | rank=%d pair=%s edge=%.1fbps net=%.1fbps score=%.2f route_bonus=%.1f size=$%.2f conf=%.1f%% rank_bps=%.1f",
                idx,
                opp.get("pair_label"),
                opp.get("edge_bps"),
                opp.get("net_bps"),
                float(opp.get("pair_fill_score") or 0.5),
                float(opp.get("pair_route_bonus_bps") or 0.0),
                int(opp["size_usdc"]) / 1e6,
                opp.get("confidence"),
                rank_bps,
            )
        queue_n = max(1, _env_int("CEX_DEX_EXEC_CANDIDATE_QUEUE_N", 3))
        if best is not None:
            best["rank_bps"] = float(best_rank)
            best["_candidate_queue"] = [
                {**candidate, "rank_bps": float(rank_bps)}
                for rank_bps, candidate in ranked_candidates[1:queue_n]
            ]
        if best:
            logger.info(
                "OPPORTUNITY | pair=%s edge=%.1fbps net=%.1fbps rank=%.1fbps score=%.2f route_bonus=%.1f size=$%.2f conf=%.1f%%",
                best.get("pair_label"),
                best.get("edge_bps"),
                best.get("net_bps"),
                best_rank,
                float(best.get("pair_fill_score") or 0.5),
                float(best.get("pair_route_bonus_bps") or 0.0),
                int(best["size_usdc"]) / 1e6,
                best.get("confidence"),
            )
        return best

    def _pair_lifecycle_profile(self, pair_label: str) -> dict[str, float | str | bool]:
        """Stateful per-pair lifecycle profile used to gate and size live execution."""
        state = self._pair_quality_snapshot(pair_label)
        attempts = int(state.get("attempts") or 0)
        score = self._pair_fill_score(pair_label)
        warmup = max(1, _env_int("CEX_DEX_LIFECYCLE_WARMUP_ATTEMPTS", 4))
        paper_floor = max(0.0, min(1.0, _env_float("CEX_DEX_LIFECYCLE_PAPER_SCORE", 0.20)))
        micro_floor = max(0.0, min(1.0, _env_float("CEX_DEX_LIFECYCLE_MICRO_SCORE", 0.45)))

        mode = "full"
        allow_live = True
        if attempts < warmup:
            mode = "observe"
        elif score < paper_floor:
            mode = "paper"
            allow_live = _env_bool("CEX_DEX_LIFECYCLE_PAPER_ALLOW_LIVE", False)
        elif score < micro_floor:
            mode = "micro"

        observe_cap = max(2.0, _env_float("CEX_DEX_LIFECYCLE_OBSERVE_MAX_USDC", 8.0))
        micro_cap = max(observe_cap, _env_float("CEX_DEX_LIFECYCLE_MICRO_MAX_USDC", 14.0))
        paper_cap = max(1.0, _env_float("CEX_DEX_LIFECYCLE_PAPER_MAX_USDC", 5.0))
        observe_extra = max(0.0, _env_float("CEX_DEX_LIFECYCLE_OBSERVE_EXTRA_NET_BPS", 0.5))
        micro_extra = max(0.0, _env_float("CEX_DEX_LIFECYCLE_MICRO_EXTRA_NET_BPS", 0.25))
        paper_extra = max(0.0, _env_float("CEX_DEX_LIFECYCLE_PAPER_EXTRA_NET_BPS", 1.0))

        if mode == "observe":
            return {
                "mode": mode,
                "allow_live": allow_live,
                "max_size_usdc": observe_cap,
                "extra_net_bps": observe_extra,
            }
        if mode == "micro":
            return {
                "mode": mode,
                "allow_live": allow_live,
                "max_size_usdc": micro_cap,
                "extra_net_bps": micro_extra,
            }
        if mode == "paper":
            return {
                "mode": mode,
                "allow_live": allow_live,
                "max_size_usdc": paper_cap,
                "extra_net_bps": paper_extra,
            }
        return {
            "mode": "full",
            "allow_live": True,
            "max_size_usdc": 0.0,
            "extra_net_bps": 0.0,
        }

    def _pair_adaptive_rescue_profile(self, pair_label: str) -> dict[str, float]:
        """Adaptive per-pair rescue thresholds from recent conversion quality."""
        state = self._pair_quality_snapshot(pair_label)
        score = self._pair_fill_score(pair_label)
        drift_ema = float(state.get("drift_ema_abs") or 0.0)

        min_net_delta = 0.0
        size_mult = 1.0
        if score < 0.35:
            min_net_delta += _env_float("CEX_DEX_ADAPTIVE_RESCUE_LOW_SCORE_MIN_NET_DELTA", 0.35)
            size_mult *= _env_float("CEX_DEX_ADAPTIVE_RESCUE_LOW_SCORE_SIZE_MULT", 0.75)
        elif score > 0.7:
            min_net_delta += _env_float("CEX_DEX_ADAPTIVE_RESCUE_HIGH_SCORE_MIN_NET_DELTA", -0.20)
            size_mult *= _env_float("CEX_DEX_ADAPTIVE_RESCUE_HIGH_SCORE_SIZE_MULT", 1.05)

        if drift_ema >= _env_float("CEX_DEX_ADAPTIVE_RESCUE_HIGH_DRIFT_BPS", 16.0):
            min_net_delta += _env_float("CEX_DEX_ADAPTIVE_RESCUE_HIGH_DRIFT_MIN_NET_DELTA", 0.25)
            size_mult *= _env_float("CEX_DEX_ADAPTIVE_RESCUE_HIGH_DRIFT_SIZE_MULT", 0.85)

        return {
            "min_net_delta_bps": float(min_net_delta),
            "size_mult": max(0.25, min(1.5, float(size_mult))),
        }

    def _trade_min_micro(self) -> int:
        """Resolve minimum trade floor with direct CEX-DEX override when set."""
        raw = (os.getenv("CEX_DEX_MIN_TRADE_USDC_MICRO") or "").strip()
        if raw:
            try:
                return max(1, int(raw))
            except ValueError:
                pass
        return int(self.settings.trading.min_flash_usdc * 1_000_000)

    def _exec_size_ladder(self, symbol: str, initial_size_micro: int) -> list[int]:
        """Candidate execution sizes from largest to smallest for quote decay handling."""
        min_trade = self._trade_min_micro()
        max_trade = self._max_trade_usdc_micro(symbol)
        start = max(min_trade, min(max_trade, int(initial_size_micro)))

        raw = (os.getenv("CEX_DEX_EXEC_SIZE_LADDER") or "1.0,0.75,0.5,0.35").strip()
        multipliers: list[float] = []
        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue
            try:
                mult = float(token)
            except ValueError:
                continue
            if mult > 0:
                multipliers.append(mult)
        if not multipliers:
            multipliers = [1.0, 0.75, 0.5, 0.35]

        candidates: list[int] = []
        for mult in multipliers:
            sized = int(start * mult)
            clamped = max(min_trade, min(max_trade, sized))
            if clamped not in candidates:
                candidates.append(clamped)
        return candidates

    async def _best_exec_quote(
        self,
        pair: CexDexPair,
        cex_buy: float,
        *,
        initial_size_micro: int,
    ) -> dict[str, float] | None:
        """Pick the size/quote combination with best modeled net for cex_cheap direction."""
        best: dict[str, float] | None = None
        for candidate_size in self._exec_size_ladder(pair.symbol, initial_size_micro):
            exec_jup = await self._jupiter_price_for_cex_buy(pair, candidate_size, float(cex_buy))
            if not exec_jup or exec_jup <= 0:
                continue
            spread = analyze_cex_dex_spread(cex_buy, exec_jup)
            if spread is None or spread.direction != "cex_cheap":
                continue
            edge_bps = float(bps_diff(cex_buy, exec_jup))
            net_bps = net_spread_bps_after_costs(
                edge_bps,
                candidate_size,
                direction="cex_cheap",
            )
            candidate = {
                "size_usdc_micro": float(candidate_size),
                "jup_price": float(exec_jup),
                "edge_bps": float(edge_bps),
                "net_bps": float(net_bps),
                "spread_abs_bps": float(spread.spread_bps_abs),
            }
            if best is None or candidate["net_bps"] > best["net_bps"]:
                best = candidate
        return best

    async def _detect_pair_opportunity(
        self,
        pair: CexDexPair,
        *,
        gates: dict[str, float] | None = None,
    ) -> dict[str, Any] | None:
        """CEX ask (buy) vs Jupiter USDC→base probe for one market."""
        gates = gates or await self.get_dynamic_gates()
        cex_buy, cex_mid, cex_ask = await self.backpack.get_cex_buy_reference_price(
            pair.backpack_symbol
        )
        if cex_buy and cex_buy > 0:
            from src.strategies.volatility_gate import record_cex_price

            record_cex_price(float(cex_buy), symbol=pair.symbol)
        if not cex_buy or cex_buy <= 0:
            return None

        probe_micro = self._probe_usdc_micro()
        probe_slippage = resolve_slippage_bps(USDC_MINT, pair.base_mint)
        jup_price, probe_quote = await self.jupiter.get_implied_usdc_per_base(
            probe_micro,
            pair.base_mint,
            base_decimals=pair.base_decimals,
            slippage_bps=probe_slippage,
        )
        dex_venue = "jupiter_buy"
        sell_px, sell_quote = await self.jupiter.get_implied_usdc_per_base_sell(
            probe_micro,
            pair.base_mint,
            float(cex_buy),
            base_decimals=pair.base_decimals,
        )
        if sell_px and sell_px > 0:
            jup_price = float(sell_px)
            probe_quote = sell_quote if isinstance(sell_quote, dict) else probe_quote
            dex_venue = "jupiter_sell"
        if pair.symbol == "SOL":
            from src.dex.executor import get_dex_executor

            dex_q = await get_dex_executor().get_best_dex_price(
                probe_micro,
                use_phoenix=True,
                jupiter_price=jup_price,
            )
            if dex_q and dex_q.price > 0:
                jup_price = dex_q.price
                dex_venue = dex_q.venue
        if not jup_price or jup_price <= 0:
            return None

        spread = analyze_cex_dex_spread(cex_buy, jup_price)
        if spread is None:
            return None

        edge_bps = float(bps_diff(cex_buy, jup_price))
        spread_abs = spread.spread_bps_abs
        gross_bps = edge_bps if spread.direction == "cex_cheap" else spread_abs
        probe_edge_bps = float(edge_bps)

        scan_ctx: dict[str, Any] = {
            "symbol": pair.symbol,
            "backpack_symbol": pair.backpack_symbol,
            "direction": spread.direction,
            "cex_price": cex_buy,
            "jup_price": jup_price,
            "size_usdc_micro": probe_micro,
            "edge_bps": edge_bps,
            "gross_bps": gross_bps,
            "spread_bps_gross": spread_abs,
        }
        set_cex_cheap_flags(scan_ctx, spread.direction)
        dir_reject = gate_cex_dex_direction(scan_ctx)
        if dir_reject:
            probe_net = net_spread_bps_after_costs(
                gross_bps,
                probe_micro,
                direction=spread.direction,
            )
            confidence = await self._calculate_confidence(
                int(edge_bps), int(probe_net), probe_micro
            )
            missed = {
                "gross_bps": edge_bps,
                "net_bps": probe_net,
                "ai_confidence": confidence,
                "direction": spread.direction,
                "size_usdc_micro": probe_micro,
                "pair_label": pair.pair_label,
            }
            if not self.should_log_near_miss(missed, gates=gates):
                self.log_near_miss(
                    edge_bps,
                    probe_net,
                    confidence,
                    str(dir_reject.get("status") or "direction_reject"),
                    pair=pair.pair_label,
                )
            return None

        base_size_usdc = await self._calculate_size(int(gross_bps), cex_buy, pair.symbol)
        base_size_usdc = self._clamp_opportunity_size(base_size_usdc, pair.symbol)
        best_exec = await self._best_exec_quote(
            pair,
            cex_buy,
            initial_size_micro=base_size_usdc,
        )
        if best_exec is None:
            return None
        size_usdc = int(best_exec["size_usdc_micro"])
        jup_price = float(best_exec["jup_price"])
        edge_bps = float(best_exec["edge_bps"])
        spread_abs = float(best_exec["spread_abs_bps"])
        gross_bps = edge_bps
        net_bps = float(best_exec["net_bps"])
        if size_usdc < base_size_usdc:
            logger.info(
                "Exec size adjusted | pair=%s base_usdc=%.2f tuned_usdc=%.2f exec_edge=%.1f net=%.1f",
                pair.pair_label,
                base_size_usdc / 1_000_000.0,
                size_usdc / 1_000_000.0,
                edge_bps,
                net_bps,
            )

        min_trade_micro = dynamic_min_trade_usdc_micro(
            gross_bps,
            settings=self.settings,
        )
        if size_usdc < min_trade_micro:
            confidence = await self._calculate_confidence(
                int(edge_bps), int(net_bps), size_usdc
            )
            self.log_near_miss(
                edge_bps,
                net_bps,
                confidence,
                "size_below_min_trade",
                pair=pair.pair_label,
            )
            return None

        scan_ctx["size_usdc_micro"] = size_usdc
        depth_reject = await gate_cex_dex_ask_depth(self.backpack, scan_ctx)
        if depth_reject:
            confidence = await self._calculate_confidence(
                int(edge_bps), int(net_bps), size_usdc
            )
            self.log_near_miss(
                edge_bps,
                net_bps,
                confidence,
                depth_reject["status"],
                pair=pair.pair_label,
            )
            return None

        confidence = await self._calculate_confidence(
            int(edge_bps), int(net_bps), size_usdc
        )

        cost_bps = modeled_roundtrip_cost_bps(size_usdc)
        logger.info(
            "CEX-DEX Scan | pair=%s probe_edge=%.1f exec_edge=%.1f spread_abs=%.1f "
            "cost_bps=%.1f net_bps=%.1f dir=%s confidence=%.1f size_usdc=%d probe=%d",
            pair.pair_label,
            probe_edge_bps,
            edge_bps,
            spread_abs,
            cost_bps,
            net_bps,
            spread.direction,
            confidence,
            size_usdc,
            probe_micro,
        )
        record_probe_exec_decay(pair.pair_label, probe_edge_bps, edge_bps)

        if not self._is_sane_opportunity(edge_bps, net_bps, size_usdc, pair.symbol):
            logger.info(
                "MODEL_NET_SOFT_RESCUE_BYPASS | pair=%s reason=sanity_reject edge=%.1f net=%.1f size_usdc=%.2f",
                pair.pair_label,
                edge_bps,
                net_bps,
                size_usdc / 1_000_000.0,
            )
            self.log_near_miss(
                edge_bps, net_bps, confidence, "sanity_reject", pair=pair.pair_label
            )
            return None

        initially_profitable = self._is_profitable_opportunity(
            edge_bps,
            net_bps,
            confidence,
            gates=gates,
        )
        if initially_profitable:
            logger.info(
                "MODEL_NET_SOFT_RESCUE_BYPASS | pair=%s reason=already_profitable edge=%.1f net=%.1f conf=%.1f",
                pair.pair_label,
                edge_bps,
                net_bps,
                confidence,
            )

        rescued_by_roundtrip = False
        rescue_negative_sim = False
        if not initially_profitable:
            logger.info(
                "MODEL_NET_SOFT_RESCUE_ELIGIBLE | pair=%s edge=%.1f net=%.1f conf=%.1f test_mode=%s simulate=%s",
                pair.pair_label,
                edge_bps,
                net_bps,
                confidence,
                self.settings.test_mode,
                self.settings.simulate,
            )
            rescue_enabled = _env_bool("CEX_DEX_MODEL_NET_SOFT_RESCUE", True)
            min_gross_gate = float(gates.get("min_gross", self.settings.CEX_DEX_MIN_GROSS_SPREAD_BPS))
            rescue_floor = float(
                os.getenv(
                    "CEX_DEX_MODEL_NET_SOFT_RESCUE_MIN_GROSS_BPS",
                    str(max(min_gross_gate, 8.0)),
                )
            )
            rescue_symbols = _soft_rescue_symbols()
            if (
                rescue_enabled
                and pair.symbol.upper() in rescue_symbols
                and edge_bps >= rescue_floor
                and not self.settings.test_mode
                and not self.settings.simulate
            ):
                adaptive_rescue = self._pair_adaptive_rescue_profile(pair.pair_label)
                rescue_min_net = _soft_rescue_min_sim_net_bps() + float(
                    adaptive_rescue.get("min_net_delta_bps") or 0.0
                )
                rescue_negative_guardrails = _soft_rescue_negative_sim_guardrails(pair.symbol)
                rescue_sizes: list[int] = []
                max_trade = self._max_trade_usdc_micro(pair.symbol)
                rescue_min_trade_micro = _env_int(
                    "CEX_DEX_MODEL_NET_SOFT_RESCUE_MIN_TRADE_USDC_MICRO",
                    int(min_trade_micro),
                )
                rescue_min_trade_micro = max(1, min(max_trade, rescue_min_trade_micro))
                start_size = max(rescue_min_trade_micro, min(max_trade, int(size_usdc)))
                start_size = int(
                    max(
                        rescue_min_trade_micro,
                        min(
                            max_trade,
                            int(start_size * float(adaptive_rescue.get("size_mult") or 1.0)),
                        ),
                    )
                )
                for mult in _soft_rescue_size_ladder():
                    candidate = int(start_size * mult)
                    clamped = max(rescue_min_trade_micro, min(max_trade, candidate))
                    if clamped not in rescue_sizes:
                        rescue_sizes.append(clamped)
                for depth_hint in await self._rescue_depth_size_hints(
                    pair,
                    start_size_micro=start_size,
                    rescue_min_trade_micro=rescue_min_trade_micro,
                    max_trade_micro=max_trade,
                ):
                    if depth_hint not in rescue_sizes:
                        rescue_sizes.append(depth_hint)
                if bool(rescue_negative_guardrails.get("enabled", False)):
                    # Ensure the near-breakeven max-size band is always evaluated explicitly.
                    cap_usdc = float(rescue_negative_guardrails.get("max_size_usdc", 0.0))
                    cap_micro = int(cap_usdc * 1_000_000.0)
                    if cap_micro > 0:
                        cap_size = max(
                            rescue_min_trade_micro,
                            min(max_trade, start_size, cap_micro),
                        )
                        if cap_size not in rescue_sizes:
                            rescue_sizes.append(cap_size)
                rescue_sizes = sorted(set(rescue_sizes), reverse=True)

                sim_reason = "no_rescue_size"
                best_fail_sim = float("-inf")
                best_fail_size = int(size_usdc)
                best_fail_sim_usdc_micro = int(size_usdc)
                best_fail_depth_market = pair.backpack_symbol
                chosen_rescue_size = int(size_usdc)
                chosen_sim_net = float(net_bps)
                chosen_sim_usdc_micro = int(size_usdc)
                chosen_depth_market = pair.backpack_symbol
                chosen_sim_details: dict[str, Any] = {}
                detect_ts_ms = int(time.time() * 1000)
                rescue_negative_sim = False
                rescue_negative_blocked_reason = ""
                rescue_timeout_sec = _env_float("CEX_DEX_MODEL_NET_SOFT_RESCUE_SIM_TIMEOUT_SEC", 3.5)
                base_timeout_retries = max(
                    1,
                    _env_int("CEX_DEX_MODEL_NET_SOFT_RESCUE_SIM_TIMEOUT_RETRIES", 1),
                )
                extra_retries = 0
                if edge_bps >= _env_float("CEX_DEX_MODEL_NET_SOFT_RESCUE_TIMEOUT_EDGE_BPS", 35.0):
                    extra_retries += 1
                if str(gates.get("mode") or "") in ("opportunistic", "aggressive"):
                    extra_retries += 1
                pair_cfg = (_load_rescue_pair_policy().get("pairs") or {}).get(pair.symbol.upper(), {})
                if isinstance(pair_cfg, dict):
                    try:
                        extra_retries += max(0, int(pair_cfg.get("sim_timeout_retries", 0)))
                    except (TypeError, ValueError):
                        pass
                rescue_timeout_retries = min(
                    max(base_timeout_retries, 1) + extra_retries,
                    max(1, _env_int("CEX_DEX_MODEL_NET_SOFT_RESCUE_SIM_TIMEOUT_MAX_RETRIES", 4)),
                )
                try:
                    for rescue_size in rescue_sizes:
                        rescue_probe_micro = min(self._probe_usdc_micro(), int(rescue_size))
                        sim_details: dict[str, Any] = {}
                        sim_ok = False
                        sim_net = float("-inf")
                        sim_reason = "no_sim_result"
                        for sim_attempt in range(rescue_timeout_retries):
                            try:
                                sim_ok, sim_net, sim_reason, sim_details = await asyncio.wait_for(
                                    pre_simulate_cex_buy_dex_sell(
                                        self.jupiter,
                                        int(rescue_size),
                                        float(cex_buy),
                                        backpack_symbol=pair.backpack_symbol,
                                        base_mint=pair.base_mint,
                                        base_decimals=pair.base_decimals,
                                        expected_net_bps=float(net_bps),
                                        probe_usdc_micro=rescue_probe_micro,
                                        min_net_bps=rescue_min_net,
                                    ),
                                    timeout=max(0.5, float(rescue_timeout_sec)),
                                )
                                break
                            except TimeoutError:
                                sim_ok = False
                                sim_net = float("-inf")
                                sim_reason = f"timeout_{rescue_timeout_sec:.1f}s"
                                if sim_attempt + 1 >= rescue_timeout_retries:
                                    break
                            except Exception as exc:
                                sim_ok = False
                                sim_net = float("-inf")
                                sim_reason = f"exception:{type(exc).__name__}"
                                logger.warning(
                                    "MODEL_NET_SOFT_RESCUE_ATTEMPT_FAIL | pair=%s size_usdc=%.2f reason=%s error=%s",
                                    pair.pair_label,
                                    int(rescue_size) / 1_000_000.0,
                                    sim_reason,
                                    exc,
                                )
                                break
                        if sim_ok:
                            chosen_rescue_size = int(rescue_size)
                            chosen_sim_net = float(sim_net)
                            chosen_sim_usdc_micro = int(
                                sim_details.get("usdc_in_micro") or rescue_size
                            )
                            chosen_sim_details = dict(sim_details)
                            chosen_depth_market = str(
                                sim_details.get("cex_depth_market") or pair.backpack_symbol
                            )
                            rescued_by_roundtrip = True
                            break
                        allow_negative = bool(rescue_negative_guardrails.get("enabled", False))
                        max_loss = float(rescue_negative_guardrails.get("max_loss_bps", 0.0))
                        calibrated_conf = _rescue_recalibrated_confidence(
                            confidence,
                            pair.symbol,
                            float(sim_net),
                        )
                        negative_gate_ok = (
                            allow_negative
                            and str(sim_reason).startswith("net_below_")
                            and float(sim_net) >= -max_loss
                            and edge_bps >= float(rescue_negative_guardrails.get("min_edge_bps", 0.0))
                            and calibrated_conf
                            >= float(rescue_negative_guardrails.get("min_ai_conf", 0.0))
                            and (int(rescue_size) / 1_000_000.0)
                            <= float(rescue_negative_guardrails.get("max_size_usdc", 0.5))
                        )
                        if negative_gate_ok:
                            budget_ok, budget_reason = self._negative_tier_budget_allows()
                            if not budget_ok:
                                rescue_negative_blocked_reason = budget_reason
                                record_rescue_negative_event(
                                    pair=pair.pair_label,
                                    outcome="blocked",
                                    sim_net_bps=float(sim_net),
                                    edge_bps=float(edge_bps),
                                    size_usdc=float(int(rescue_size) / 1_000_000.0),
                                    blocked_by=budget_reason,
                                )
                                continue
                            chosen_rescue_size = int(rescue_size)
                            chosen_sim_net = float(sim_net)
                            chosen_sim_usdc_micro = int(
                                sim_details.get("usdc_in_micro") or rescue_size
                            )
                            chosen_sim_details = dict(sim_details)
                            chosen_depth_market = str(
                                sim_details.get("cex_depth_market") or pair.backpack_symbol
                            )
                            sim_reason = "soft_negative_band"
                            rescue_negative_sim = True
                            rescued_by_roundtrip = True
                            confidence = calibrated_conf
                            record_rescue_negative_event(
                                pair=pair.pair_label,
                                outcome="pass",
                                sim_net_bps=chosen_sim_net,
                                edge_bps=float(edge_bps),
                                size_usdc=float(chosen_rescue_size / 1_000_000.0),
                            )
                            logger.warning(
                                "MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_PASS | pair=%s edge=%.1f sim_net=%.2f conf=%.1f size_usdc=%.2f",
                                pair.pair_label,
                                edge_bps,
                                chosen_sim_net,
                                calibrated_conf,
                                chosen_rescue_size / 1_000_000.0,
                            )
                            break
                        if float(sim_net) > best_fail_sim:
                            best_fail_sim = float(sim_net)
                            best_fail_size = int(rescue_size)
                            best_fail_sim_usdc_micro = int(
                                sim_details.get("usdc_in_micro") or rescue_size
                            )
                            best_fail_depth_market = str(
                                sim_details.get("cex_depth_market") or pair.backpack_symbol
                            )
                except Exception as exc:
                    sim_reason = f"exception:{type(exc).__name__}"
                    logger.warning(
                        "MODEL_NET_SOFT_RESCUE_SKIP | pair=%s edge=%.1f reason=%s error=%s",
                        pair.pair_label,
                        edge_bps,
                        sim_reason,
                        exc,
                    )
                else:
                    if rescued_by_roundtrip:
                        logger.info(
                            "MODEL_NET_SOFT_RESCUE | pair=%s edge=%.1f modeled_net=%.1f sim_net=%.1f rescue_size_usdc=%.2f sim_usdc=%.2f cex_depth_market=%s negative_sim=%s",
                            pair.pair_label,
                            edge_bps,
                            net_bps,
                            chosen_sim_net,
                            chosen_rescue_size / 1_000_000.0,
                            chosen_sim_usdc_micro / 1_000_000.0,
                            chosen_depth_market,
                            rescue_negative_sim,
                        )
                        size_usdc = int(chosen_rescue_size)
                        net_bps = max(net_bps, chosen_sim_net)
                    else:
                        fail_preview = "n/a" if best_fail_sim == float("-inf") else f"{best_fail_sim:.2f}"
                        logger.info(
                            "MODEL_NET_SOFT_RESCUE_SKIP | pair=%s edge=%.1f reason=%s best_sim_net=%sbps best_size_usdc=%.2f sim_usdc=%.2f cex_depth_market=%s",
                            pair.pair_label,
                            edge_bps,
                            sim_reason,
                            fail_preview,
                            best_fail_size / 1_000_000.0,
                            best_fail_sim_usdc_micro / 1_000_000.0,
                            best_fail_depth_market,
                        )
                        allow_negative = bool(rescue_negative_guardrails.get("enabled", False))
                        max_loss = float(rescue_negative_guardrails.get("max_loss_bps", 0.0))
                        if (
                            allow_negative
                            and best_fail_sim != float("-inf")
                            and -max_loss <= float(best_fail_sim) < 0.0
                        ):
                            block_reasons: list[str] = []
                            if edge_bps < float(rescue_negative_guardrails.get("min_edge_bps", 0.0)):
                                block_reasons.append("edge")
                            if confidence < float(rescue_negative_guardrails.get("min_ai_conf", 0.0)):
                                block_reasons.append("conf")
                            if (best_fail_size / 1_000_000.0) > float(
                                rescue_negative_guardrails.get("max_size_usdc", 0.5)
                            ):
                                block_reasons.append("size")
                            if block_reasons:
                                record_rescue_negative_event(
                                    pair=pair.pair_label,
                                    outcome="blocked",
                                    sim_net_bps=float(best_fail_sim),
                                    edge_bps=float(edge_bps),
                                    size_usdc=float(best_fail_size / 1_000_000.0),
                                    blocked_by=",".join(block_reasons),
                                )
                                logger.info(
                                    "MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_BLOCKED | pair=%s sim_net=%.2f edge=%.1f conf=%.1f size_usdc=%.2f blocked_by=%s thresholds=edge>=%.1f,conf>=%.1f,size<=%.2f,loss<=%.1f",
                                    pair.pair_label,
                                    best_fail_sim,
                                    edge_bps,
                                    confidence,
                                    best_fail_size / 1_000_000.0,
                                    ",".join(block_reasons),
                                    float(rescue_negative_guardrails.get("min_edge_bps", 0.0)),
                                    float(rescue_negative_guardrails.get("min_ai_conf", 0.0)),
                                    float(rescue_negative_guardrails.get("max_size_usdc", 0.5)),
                                    max_loss,
                                )
                        elif rescue_negative_blocked_reason:
                            logger.info(
                                "MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_BLOCKED | pair=%s sim_net=%s edge=%.1f conf=%.1f size_usdc=%.2f blocked_by=%s",
                                pair.pair_label,
                                fail_preview,
                                edge_bps,
                                confidence,
                                best_fail_size / 1_000_000.0,
                                rescue_negative_blocked_reason,
                            )
            rescue_bypass_net = _env_bool("CEX_DEX_MODEL_NET_SOFT_RESCUE_BYPASS_NET_GATE", True)
            rescued_gate_ok = False
            if rescued_by_roundtrip and rescue_bypass_net:
                min_gross = float(gates.get("min_gross", self.settings.CEX_DEX_MIN_GROSS_SPREAD_BPS))
                min_ai = float(gates.get("ai_conf", self.settings.AI_APPROVE_MIN_CONFIDENCE))
                rescued_gate_ok = edge_bps >= min_gross and confidence >= min_ai

            if not rescued_gate_ok and not self._is_profitable_opportunity(edge_bps, net_bps, confidence, gates=gates):
                if self.settings.CEX_DEX_AGGRESSIVE_OPPORTUNITY_FILTER:
                    self.log_near_miss(
                        edge_bps,
                        net_bps,
                        confidence,
                        "aggressive_filter",
                        pair=pair.pair_label,
                    )
                else:
                    missed = {
                        "gross_bps": edge_bps,
                        "net_bps": net_bps,
                        "ai_confidence": confidence,
                        "direction": spread.direction,
                        "size_usdc_micro": size_usdc,
                        "pair_label": pair.pair_label,
                    }
                    if not self.should_log_near_miss(missed, gates=gates):
                        reason = (
                            f"env_thresholds_{gates.get('mode', 'strict')}"
                            if gates.get("mode") == "opportunistic"
                            else "env_thresholds"
                        )
                        self.log_near_miss(
                            edge_bps,
                            net_bps,
                            confidence,
                            reason,
                            pair=pair.pair_label,
                        )
                return None

        if (
            pair.symbol == "SOL"
            and _env_bool("CEX_DEX_ROUNDTRIP_AT_DETECT", True)
            and not self.settings.test_mode
            and not self.settings.simulate
        ):
            sim_ok, sim_net, sim_reason, _ = await pre_simulate_cex_buy_dex_sell(
                self.jupiter,
                int(size_usdc),
                float(cex_buy),
                backpack_symbol=pair.backpack_symbol,
                base_mint=pair.base_mint,
                base_decimals=pair.base_decimals,
                expected_net_bps=float(net_bps),
                probe_usdc_micro=self._probe_usdc_micro(),
                min_net_bps=roundtrip_sim_min_net_bps(),
            )
            if not sim_ok:
                self.log_near_miss(
                    edge_bps,
                    net_bps,
                    confidence,
                    f"detect_roundtrip:{sim_reason}",
                    pair=pair.pair_label,
                )
                return None

        record_trade_signal(
            "cex_dex",
            float(net_bps),
            float(size_usdc),
            confidence,
            gross_bps=edge_bps,
        )
        detect_route_hops = quote_route_hops(probe_quote) if isinstance(probe_quote, dict) else 0
        detect_price_impact_bps = None
        if isinstance(probe_quote, dict):
            try:
                pct = probe_quote.get("priceImpactPct")
                if pct is not None:
                    detect_price_impact_bps = abs(float(pct)) * 10_000.0
            except (TypeError, ValueError):
                detect_price_impact_bps = None
        rescue_quote_return_bps = None
        if chosen_sim_details:
            in_micro = int(chosen_sim_details.get("usdc_in_micro") or 0)
            out_micro = int(chosen_sim_details.get("usdc_back_micro") or 0)
            if in_micro > 0 and out_micro > 0:
                rescue_quote_return_bps = ((out_micro - in_micro) / in_micro) * 10_000.0
        return {
            "symbol": pair.symbol,
            "pair_label": pair.pair_label,
            "backpack_symbol": pair.backpack_symbol,
            "base_mint": pair.base_mint,
            "base_decimals": pair.base_decimals,
            "gross_bps": int(round(edge_bps)),
            "net_bps": int(round(net_bps)),
            "edge_bps": edge_bps,
            "spread_abs_bps": spread_abs,
            "direction": spread.direction,
            "is_cex_cheap": True,
            "size_usdc": size_usdc,
            "size_usdc_micro": size_usdc,
            "confidence": confidence,
            "cex_price": cex_buy,
            "cex_mid": cex_mid,
            "cex_ask": cex_ask,
            "jup_price": jup_price,
            "dex_venue": dex_venue,
            "jup_probe_quote": probe_quote,
            "detect_route_hops": int(detect_route_hops),
            "detect_price_impact_bps": detect_price_impact_bps,
            "gate_mode": gates.get("mode", "strict"),
            "dynamic_gates": gates,
            "rescue_negative_sim": bool(rescue_negative_sim),
            "detect_ts_ms": detect_ts_ms if "detect_ts_ms" in locals() else int(time.time() * 1000),
            "rescue_sim_net_bps": float(chosen_sim_net) if rescued_by_roundtrip else None,
            "rescue_roundtrip_min_bps": (
                float(chosen_sim_details.get("min_net_bps")) if chosen_sim_details else None
            ),
            "rescue_sim_usdc_micro": int(chosen_sim_usdc_micro) if rescued_by_roundtrip else None,
            "rescue_cex_effective_buy_price": (
                float(chosen_sim_details.get("cex_effective_buy_price"))
                if chosen_sim_details.get("cex_effective_buy_price") is not None
                else None
            ),
            "rescue_quote_return_bps": rescue_quote_return_bps,
            "rescue_negative_pair_policy": bool(
                isinstance(((_load_rescue_pair_policy().get("pairs") or {}).get(pair.symbol.upper())), dict)
            ),
        }

    async def get_safe_trade_size(self) -> int:
        """Return micro USDC amount safe for current Backpack + on-chain balances."""
        backpack_usdc = await self.backpack.get_balance("USDC")
        onchain_usdc = await get_onchain_usdc_balance()
        cex_util = float(os.getenv("CEX_SAFE_SIZE_UTILIZATION", "0.85"))
        chain_util = float(os.getenv("ONCHAIN_SAFE_SIZE_UTILIZATION", "0.9"))
        available_usdc = min(backpack_usdc * cex_util, onchain_usdc * chain_util)
        max_usdc = float(self.settings.trading.max_flash_usdc)
        safe_usdc = min(available_usdc, max_usdc)
        return int(max(0.0, safe_usdc) * 1_000_000)

    def _max_trade_usdc_micro(self, symbol: str | None = None) -> int:
        sym = (symbol or "SOL").strip().upper()
        max_micro = get_max_trade_size_micro(sym)
        global_max = int(self.settings.trading.max_flash_usdc * 1_000_000)
        return min(max_micro, global_max)

    def _clamp_opportunity_size(self, size_micro: int, symbol: str | None = None) -> int:
        sym = (symbol or "SOL").strip().upper()
        min_micro = self._trade_min_micro()
        flash_cap = min(
            get_max_trade_size_micro(sym),
            int(self.settings.trading.max_flash_usdc * 1_000_000),
        )
        return clamp_trade_usdc_micro(
            max_trade_usdc_micro=self._max_trade_usdc_micro(symbol),
            flash_cap_usdc_micro=flash_cap,
            liquidity_cap_usdc_micro=size_micro,
            min_trade_usdc_micro=min_micro,
        )

    def _is_sane_opportunity(
        self,
        edge_bps: float,
        net_bps: float,
        size_micro: int,
        symbol: str | None = None,
    ) -> bool:
        """Reject fantasy spreads/sizes from bad quotes or stale config."""
        max_net = float(os.getenv("CEX_DEX_MAX_MODELED_NET_BPS", "80"))
        max_gross = float(os.getenv("CEX_DEX_MAX_MODELED_GROSS_BPS", "120"))
        if edge_bps > max_gross or net_bps > max_net:
            return False
        if size_micro <= 0 or size_micro > self._max_trade_usdc_micro(symbol):
            return False
        return True

    def calculate_trade_size(self, pair_symbol: str, gross_bps: float) -> int:
        """Per-pair cap with edge scaling (delegates to src.core.sizing)."""
        return sizing_calculate_trade_size(
            pair_symbol,
            gross_bps,
            global_max_usdc=float(self.settings.trading.max_flash_usdc),
        )

    async def _calculate_size(
        self,
        gross_bps: int,
        cex_price: float,
        symbol: str | None = None,
    ) -> int:
        _ = cex_price
        pair_symbol = (symbol or "SOL").strip().upper()
        size_usdc = self.calculate_trade_size(pair_symbol, float(gross_bps))
        pair_label = f"{pair_symbol}/USDC"
        size_mult = self._pair_size_multiplier(pair_label)
        size_usdc = int(max(self._trade_min_micro(), int(size_usdc * size_mult)))

        if self.settings.trading.dynamic_amount:
            safe_cap = await self.get_safe_trade_size()
            if safe_cap > 0:
                size_usdc = min(size_usdc, safe_cap)
            else:
                size_usdc = 0
        return size_usdc

    async def _calculate_confidence(self, gross: int, net: int, size: int) -> float:
        """Brain + LightGBM ensemble (falls back to heuristic if ML unavailable)."""
        from src.ai.ensemble_scorer import score_opportunity

        try:
            conf, _reason = await score_opportunity(
                gross_bps=float(gross),
                net_bps=float(net),
                size_usdc_micro=int(size),
            )
            return float(conf)
        except Exception as exc:
            logger.debug("ensemble score fallback: %s", exc)
            from src.ai.ensemble_scorer import heuristic_confidence

            return heuristic_confidence(float(gross), float(net), int(size))

    def _wallet_pubkey(self) -> str:
        return str(
            self.settings.wallet_pubkey
            or self.settings.WALLET_PUBKEY
            or os.getenv("WALLET_PUBKEY", "")
        ).strip()

    def _log_blocked_attempt(self, opp: dict[str, Any], reason: str) -> None:
        """Persist blocked execution attempts for monitoring / later ML."""
        if self.settings.test_mode or self.settings.simulate:
            return
        try:
            from src.execution.trade_logger import log_blocked_attempt

            log_blocked_attempt(
                pair=str(opp.get("pair_label") or "SOL/USDC"),
                gross_bps=float(opp.get("gross_bps") or opp.get("edge_bps") or 0),
                net_bps=float(opp.get("net_bps") or 0),
                size_usdc=int(opp.get("size_usdc") or 0) / 1_000_000.0,
                block_reason=reason,
            )
        except Exception as exc:
            logger.debug("log_blocked_attempt skipped: %s", exc)

    def _pair_roundtrip_cooling(self, pair_label: str) -> tuple[bool, str]:
        label = (pair_label or "UNKNOWN").strip().upper() or "UNKNOWN"
        state = self._pair_roundtrip_fail_state.get(label)
        if not state:
            return False, ""
        streak = int(state.get("streak") or 0)
        limit = max(1, _env_int("CEX_DEX_EXEC_ROUNDTRIP_FAIL_STREAK", 3))
        if streak < limit:
            return False, ""
        cooldown_sec = max(30, _env_int("CEX_DEX_EXEC_ROUNDTRIP_FAIL_COOLDOWN_SEC", 900))
        last_ts = float(state.get("last_ts") or 0.0)
        age = time.time() - last_ts
        if age >= cooldown_sec:
            return False, ""
        return True, f"roundtrip_fail_streak_{streak}/{limit}_cooldown_{int(cooldown_sec-age)}s"

    def _pair_roundtrip_record_fail(self, pair_label: str, sim_net_bps: float) -> None:
        label = (pair_label or "UNKNOWN").strip().upper() or "UNKNOWN"
        state = self._pair_roundtrip_fail_state.get(label, {"streak": 0})
        streak = int(state.get("streak") or 0) + 1
        self._pair_roundtrip_fail_state[label] = {
            "streak": streak,
            "last_ts": time.time(),
            "last_sim_net_bps": float(sim_net_bps),
        }

    def _pair_roundtrip_record_success(self, pair_label: str, sim_net_bps: float) -> None:
        label = (pair_label or "UNKNOWN").strip().upper() or "UNKNOWN"
        self._pair_roundtrip_fail_state[label] = {
            "streak": 0,
            "last_ts": time.time(),
            "last_sim_net_bps": float(sim_net_bps),
        }

    async def _inventory_snapshot(self) -> dict[str, float]:
        try:
            onchain_usdc = float(await get_onchain_usdc_balance())
        except Exception:
            onchain_usdc = 0.0
        try:
            wallet_sol = float(await self.get_wallet_sol_balance())
        except Exception:
            wallet_sol = 0.0
        try:
            backpack_usdc = float(await self.backpack.get_balance("USDC"))
        except Exception:
            backpack_usdc = 0.0
        return {
            "onchain_usdc": onchain_usdc,
            "wallet_sol": wallet_sol,
            "backpack_usdc": backpack_usdc,
        }

    async def _check_cex_stability(
        self,
        backpack_symbol: str,
        *,
        drift_cap_override_bps: float | None = None,
    ) -> tuple[bool, float, str]:
        if not _env_bool("CEX_DEX_EXEC_CEX_STABILITY_ENABLED", True):
            cex_buy, _, _ = await self.backpack.get_cex_buy_reference_price(backpack_symbol)
            return bool(cex_buy and cex_buy > 0), float(cex_buy or 0.0), "disabled"

        snaps = max(2, _env_int("CEX_DEX_EXEC_CEX_STABILITY_SNAPSHOTS", 3))
        gap_ms = max(50, _env_int("CEX_DEX_EXEC_CEX_STABILITY_GAP_MS", 150))
        drift_cap = max(0.5, _env_float("CEX_DEX_EXEC_CEX_STABILITY_MAX_DRIFT_BPS", 4.0))
        if drift_cap_override_bps is not None:
            drift_cap = max(0.5, float(drift_cap_override_bps))
        prices: list[float] = []

        for i in range(snaps):
            cex_buy, _, _ = await self.backpack.get_cex_buy_reference_price(backpack_symbol)
            px = float(cex_buy or 0.0)
            if px <= 0:
                return False, 0.0, "cex_price_unavailable"
            prices.append(px)
            if i + 1 < snaps:
                await asyncio.sleep(gap_ms / 1000.0)

        px_min = min(prices)
        px_max = max(prices)
        px_mid = prices[-1]
        drift_bps = ((px_max - px_min) / px_mid) * 10_000.0 if px_mid > 0 else 9999.0
        if drift_bps > drift_cap:
            return False, px_mid, f"cex_unstable_{drift_bps:.2f}bps"
        return True, px_mid, "stable"

    def _log_inventory_shift(
        self,
        pair_label: str,
        *,
        before: dict[str, float],
        after: dict[str, float],
        path: str,
    ) -> None:
        logger.info(
            "INVENTORY_SHIFT | pair=%s path=%s d_onchain_usdc=%.4f d_wallet_sol=%.6f d_backpack_usdc=%.4f",
            pair_label,
            path,
            float(after.get("onchain_usdc", 0.0) - before.get("onchain_usdc", 0.0)),
            float(after.get("wallet_sol", 0.0) - before.get("wallet_sol", 0.0)),
            float(after.get("backpack_usdc", 0.0) - before.get("backpack_usdc", 0.0)),
        )

    def _pair_quality_snapshot(self, pair_label: str) -> dict[str, float | int]:
        label = (pair_label or "UNKNOWN").strip().upper() or "UNKNOWN"
        return dict(
            self._pair_quality_state.get(
                label,
                {
                    "attempts": 0,
                    "fills": 0,
                    "roundtrip_blocks": 0,
                    "drift_ema_abs": 0.0,
                },
            )
        )

    def _pair_quality_record_attempt(self, pair_label: str) -> None:
        label = (pair_label or "UNKNOWN").strip().upper() or "UNKNOWN"
        state = self._pair_quality_snapshot(label)
        state["attempts"] = int(state.get("attempts") or 0) + 1
        self._pair_quality_state[label] = state

    def _pair_quality_record_fill(self, pair_label: str) -> None:
        label = (pair_label or "UNKNOWN").strip().upper() or "UNKNOWN"
        state = self._pair_quality_snapshot(label)
        state["fills"] = int(state.get("fills") or 0) + 1
        self._pair_quality_state[label] = state

    def _pair_quality_record_roundtrip_block(
        self,
        pair_label: str,
        *,
        rescue_sim_bps: float | None,
        exec_sim_bps: float,
    ) -> None:
        label = (pair_label or "UNKNOWN").strip().upper() or "UNKNOWN"
        state = self._pair_quality_snapshot(label)
        state["roundtrip_blocks"] = int(state.get("roundtrip_blocks") or 0) + 1
        drift_abs = 0.0
        if rescue_sim_bps is not None:
            drift_abs = abs(float(rescue_sim_bps) - float(exec_sim_bps))
        alpha = max(0.05, min(1.0, _env_float("CEX_DEX_PAIR_QUALITY_DRIFT_ALPHA", 0.25)))
        prev = float(state.get("drift_ema_abs") or 0.0)
        state["drift_ema_abs"] = (alpha * drift_abs) + ((1.0 - alpha) * prev)
        self._pair_quality_state[label] = state

    def _pair_fill_score(self, pair_label: str) -> float:
        state = self._pair_quality_snapshot(pair_label)
        attempts = int(state.get("attempts") or 0)
        fills = int(state.get("fills") or 0)
        blocks = int(state.get("roundtrip_blocks") or 0)
        drift_ema = float(state.get("drift_ema_abs") or 0.0)

        min_samples = max(1, _env_int("CEX_DEX_PAIR_SCORE_MIN_SAMPLES", 5))
        if attempts < min_samples:
            return 0.5

        fill_rate = max(0.0, min(1.0, fills / max(1, attempts)))
        block_rate = max(0.0, min(1.0, blocks / max(1, attempts)))
        drift_norm = max(0.0, min(1.0, drift_ema / max(1.0, _env_float("CEX_DEX_PAIR_SCORE_DRIFT_CAP_BPS", 40.0))))

        score = (0.5 * fill_rate) + (0.3 * (1.0 - block_rate)) + (0.2 * (1.0 - drift_norm))
        return max(0.0, min(1.0, score))

    def _pair_size_multiplier(self, pair_label: str) -> float:
        score = self._pair_fill_score(pair_label)
        min_mult = max(0.2, _env_float("CEX_DEX_PAIR_SIZE_MIN_MULT", 0.6))
        max_mult = max(min_mult, _env_float("CEX_DEX_PAIR_SIZE_MAX_MULT", 1.2))
        # score=0 -> min_mult, score=1 -> max_mult
        return min_mult + ((max_mult - min_mult) * score)

    def _runtime_quality_record(
        self,
        *,
        roundtrip_blocked: bool,
        drift_abs_bps: float = 0.0,
        filled: bool = False,
    ) -> None:
        self._runtime_exec_quality.append(
            {
                "ts": float(time.time()),
                "roundtrip_blocked": bool(roundtrip_blocked),
                "drift_abs_bps": max(0.0, float(drift_abs_bps)),
                "filled": bool(filled),
            }
        )

    def _runtime_guard_profile(self) -> dict[str, float | bool]:
        if not _env_bool("CEX_DEX_RUNTIME_GUARD_ENABLED", True):
            return {
                "tightened": False,
                "block_rate": 0.0,
                "avg_drift": 0.0,
                "cex_stability_drift_cap_bps": _env_float(
                    "CEX_DEX_EXEC_CEX_STABILITY_MAX_DRIFT_BPS", 4.0
                ),
                "stage1_fraction": _env_float("CEX_DEX_EXEC_STAGE1_FRACTION", 1.0),
            }

        window_sec = max(120, _env_int("CEX_DEX_RUNTIME_GUARD_WINDOW_SEC", 1800))
        now = time.time()
        self._runtime_exec_quality = [
            e for e in self._runtime_exec_quality if now - float(e.get("ts") or 0.0) <= window_sec
        ]
        events = self._runtime_exec_quality
        if not events:
            return {
                "tightened": False,
                "block_rate": 0.0,
                "avg_drift": 0.0,
                "cex_stability_drift_cap_bps": _env_float(
                    "CEX_DEX_EXEC_CEX_STABILITY_MAX_DRIFT_BPS", 4.0
                ),
                "stage1_fraction": _env_float("CEX_DEX_EXEC_STAGE1_FRACTION", 1.0),
            }

        n = len(events)
        block_n = sum(1 for e in events if bool(e.get("roundtrip_blocked")))
        block_rate = block_n / max(1, n)
        avg_drift = sum(float(e.get("drift_abs_bps") or 0.0) for e in events) / max(1, n)

        high_block = max(0.2, _env_float("CEX_DEX_RUNTIME_GUARD_HIGH_BLOCK_RATE", 0.45))
        high_drift = max(3.0, _env_float("CEX_DEX_RUNTIME_GUARD_HIGH_DRIFT_BPS", 14.0))
        tightened = block_rate >= high_block or avg_drift >= high_drift

        base_cex_drift_cap = _env_float("CEX_DEX_EXEC_CEX_STABILITY_MAX_DRIFT_BPS", 4.0)
        tighten_factor = max(0.25, min(1.0, _env_float("CEX_DEX_RUNTIME_GUARD_TIGHTEN_FACTOR", 0.75)))
        cex_drift_cap = base_cex_drift_cap * (tighten_factor if tightened else 1.0)

        base_stage1 = _env_float("CEX_DEX_EXEC_STAGE1_FRACTION", 1.0)
        stage1_tight = max(0.35, min(base_stage1, _env_float("CEX_DEX_RUNTIME_GUARD_STAGE1_FRACTION", 0.6)))

        return {
            "tightened": tightened,
            "block_rate": block_rate,
            "avg_drift": avg_drift,
            "cex_stability_drift_cap_bps": cex_drift_cap,
            "stage1_fraction": (stage1_tight if tightened else base_stage1),
        }

    def _pair_route_pref_update(self, pair_label: str, route_hops: int) -> None:
        hops = int(route_hops or 0)
        if hops <= 0:
            return
        label = (pair_label or "UNKNOWN").strip().upper() or "UNKNOWN"
        state = dict(self._pair_route_pref.get(label, {"hops_ema": float(hops), "samples": 0.0}))
        alpha = max(0.05, min(1.0, _env_float("CEX_DEX_ROUTE_PREF_ALPHA", 0.2)))
        prev = float(state.get("hops_ema") or hops)
        state["hops_ema"] = (alpha * float(hops)) + ((1.0 - alpha) * prev)
        state["samples"] = float(state.get("samples") or 0.0) + 1.0
        self._pair_route_pref[label] = state

    def _pair_route_pref_bonus_bps(self, pair_label: str, detect_hops: int) -> float:
        label = (pair_label or "UNKNOWN").strip().upper() or "UNKNOWN"
        state = self._pair_route_pref.get(label)
        if not state or detect_hops <= 0:
            return 0.0
        samples = float(state.get("samples") or 0.0)
        min_samples = max(1, _env_int("CEX_DEX_ROUTE_PREF_MIN_SAMPLES", 5))
        if samples < min_samples:
            return 0.0
        hops_ema = float(state.get("hops_ema") or detect_hops)
        delta = abs(float(detect_hops) - hops_ema)
        penalty_per_hop = max(0.0, _env_float("CEX_DEX_ROUTE_PREF_PENALTY_BPS_PER_HOP", 1.5))
        return -(delta * penalty_per_hop)

    def _record_win_rate_outcome(
        self,
        opp: dict[str, Any],
        *,
        success: bool,
        realized_usdc: float,
        trade_id: str,
        tx_sig: str = "",
    ) -> None:
        """Independent win-rate ledger + trade history JSONL."""
        gross_bps = float(opp.get("gross_bps") or opp.get("edge_bps") or 0)
        net_bps = float(opp.get("net_bps") or 0)
        pair = str(opp.get("pair_label") or "SOL/USDC")
        size_micro = int(opp.get("size_usdc") or 0)
        size_usdc = size_micro / 1_000_000.0
        tx_signature = str(
            tx_sig or opp.get("tx_sig") or opp.get("bundle_id") or ""
        ).strip()

        self.win_rate_tracker.record_trade(
            trade_id,
            gross_bps,
            net_bps,
            realized_usdc,
            success,
            pair=pair,
        )
        if not self.settings.test_mode and not self.settings.simulate:
            try:
                from src.execution.trade_logger import log_execution_trade

                hops = int(
                    opp.get("route_hops")
                    or opp.get("jupiter_route_hops")
                    or (opp.get("jup_probe_quote") or {}).get("route_hops")
                    or 0
                )
                log_execution_trade(
                    pair=pair,
                    gross_bps=gross_bps,
                    net_bps=net_bps,
                    size_usdc=size_usdc,
                    success=success,
                    realized_usdc=realized_usdc,
                    tx_sig=tx_signature,
                    trade_id=trade_id,
                    hops=hops,
                )
            except Exception as exc:
                logger.debug("log_execution_trade skipped: %s", exc)

    async def get_wallet_sol_balance(self) -> float:
        """On-chain SOL balance for configured wallet (RPC / Ledger pubkey)."""
        return await get_ledger_sol_balance()

    @staticmethod
    def _estimate_sell_lamports(
        size_micro: int,
        cex_price: float,
        *,
        base_decimals: int = 9,
    ) -> int:
        """Raw base token amount (lamports for SOL) to match a USDC notional on CEX."""
        fudge = float(os.getenv("CEX_DEX_CEX_BUY_FILL_FUDGE", "0.995"))
        usdc = size_micro / 1_000_000.0
        base_amount = (usdc / max(cex_price, 1e-9)) * fudge
        return max(1, int(base_amount * (10**int(base_decimals))))

    async def _execute_jupiter_sell_only(
        self,
        opp: dict[str, Any],
        *,
        size_micro: int = 0,
        cex_px: float = 0.0,
        wallet: str = "",
    ) -> bool:
        """Sell on-chain SOL via Jupiter (inventory fast path)."""
        _ = wallet
        reserve_sol = float(os.getenv("CEX_DEX_SOL_SELL_RESERVE_SOL", "0.02"))
        wallet_sol = await self.get_wallet_sol_balance()
        required_lamports = int(
            opp.get("size_lamports")
            or (
                self._estimate_sell_lamports(
                    size_micro,
                    cex_px,
                    base_decimals=int(opp.get("base_decimals") or 9),
                )
                if size_micro > 0 and cex_px > 0
                else 0
            )
        )
        available_lamports = max(
            0,
            int(wallet_sol * 1_000_000_000) - int(reserve_sol * 1_000_000_000),
        )
        if required_lamports < 1 or available_lamports < required_lamports:
            logger.warning(
                "Inventory path aborted | need=%s lamports have=%s (reserve=%.4f SOL)",
                required_lamports,
                available_lamports,
                reserve_sol,
            )
            return False

        amount_lamports = min(required_lamports, available_lamports)
        opp["size_lamports"] = amount_lamports
        opp["_chain_sell_lamports"] = amount_lamports
        logger.info(
            "Using on-chain inventory (fast path) | sell_lamports=%s (~%.4f SOL)",
            amount_lamports,
            amount_lamports / 1_000_000_000.0,
        )

        slippage_bps = int(os.getenv("CEX_DEX_EXECUTION_SLIPPAGE_BUFFER_BPS", "40"))
        result = await self.jupiter_executor.sell_sol(
            amount_lamports=amount_lamports,
            slippage_bps=slippage_bps,
            net_bps=float(opp.get("net_bps") or 0),
        )
        result["live_fill"] = bool(result.get("success"))
        result["path"] = "inventory"
        opp["_inventory_sell_result"] = result
        opp["_execution_path"] = "inventory"

        tx_sig = str(
            result.get("tx_sig") or result.get("txid") or result.get("bundle_id") or ""
        )
        if tx_sig:
            opp["tx_sig"] = tx_sig
        out_usdc = int(result.get("out_usdc_micro") or 0)
        if out_usdc > 0:
            opp["_last_exec_out_usdc_micro"] = out_usdc

        return bool(result.get("success"))

    async def _check_cex_balance_before_buy(self, size_micro: int) -> bool:
        """Block live CEX buy when Backpack USDC is below trade size + buffer."""
        if self.settings.test_mode or self.settings.simulate:
            return True

        ok, available, required = await self.backpack.sufficient_usdc_for_buy(size_micro)
        if ok:
            return True

        buffer = float(os.getenv("CEX_BUY_BALANCE_BUFFER_USDC", "0.25"))
        logger.warning(
            "BLOCKED: insufficient Backpack USDC | have=$%.2f need=$%.2f buffer=$%.2f",
            available,
            required,
            buffer,
        )
        return False

    async def _execute_full_cex_dex_sol(
        self,
        opp: dict[str, Any],
        *,
        size_micro: int,
        cex_px: float,
        wallet: str,
    ) -> bool:
        """CEX market buy → withdraw SOL to wallet → Jupiter/Jito sell."""
        bp_symbol = str(opp.get("backpack_symbol") or "SOL_USDC")
        base_symbol = str(opp.get("symbol") or "SOL").strip().upper()
        base_mint = str(opp.get("base_mint") or SOL_MINT)
        base_decimals = int(opp.get("base_decimals") or 9)
        is_sol_base = base_symbol == "SOL" or base_mint == SOL_MINT
        logger.info(
            "FULL CEX-DEX %s | buying $%.2f USDC on Backpack",
            base_symbol,
            size_micro / 1e6,
        )

        sol_before = await get_ledger_sol_balance() if is_sol_base else 0.0
        withdraw_ref = await self.backpack.execute_cex_buy_then_withdraw(
            size_micro,
            pair=bp_symbol,
            destination=wallet,
        )
        if not withdraw_ref:
            logger.error("CEX buy or SOL withdraw failed")
            # Flag that no funds were disbursed — caller should not record a fill-loss penalty
            opp["_cex_pre_fill_reject"] = True
            return False

        logger.info("%s withdrawn to wallet | ref=%s", base_symbol, withdraw_ref)

        if is_sol_base:
            sol_after = await get_ledger_sol_balance()
            reserve_sol = float(os.getenv("CEX_DEX_SOL_SELL_RESERVE_SOL", "0.02"))
            delta_sol = max(0.0, sol_after - sol_before)
            sell_base = max(0.0, delta_sol - reserve_sol)
        else:
            sol_after = sol_before
            sell_base = 0.0

        if sell_base <= 0:
            fudge = float(os.getenv("CEX_DEX_CEX_BUY_FILL_FUDGE", "0.995"))
            sell_base = (size_micro / 1e6) / max(cex_px, 1e-9) * fudge
            if is_sol_base:
                sell_base = min(
                    sell_base,
                    max(
                        0.0,
                        sol_after - float(os.getenv("CEX_DEX_SOL_SELL_RESERVE_SOL", "0.02")),
                    ),
                )

        base_raw = int(sell_base * (10**base_decimals))
        if base_raw < 1:
            logger.error(
                "No on-chain %s to sell after withdraw | before=%.4f after=%.4f",
                base_symbol,
                sol_before if is_sol_base else 0.0,
                sol_after if is_sol_base else 0.0,
            )
            return False

        opp["_chain_sell_lamports"] = base_raw
        opp["_execution_path"] = "cex_withdraw"
        logger.info(
            "Jupiter sell sizing | raw=%s (~%.4f %s)",
            base_raw,
            sell_base,
            base_symbol,
        )
        return await self._execute_jupiter_sell_with_retries(
            size_micro,
            cex_px,
            wallet,
            opp,
        )

    async def execute_trade(self, opp: dict[str, Any]) -> bool:
        """Execute full CEX-DEX leg: CEX buy → Jupiter swap → Jito bundle."""
        size_micro = int(opp["size_usdc"])
        original_size_micro = int(size_micro)
        guard_profile = self._runtime_guard_profile()
        stage1_frac = float(guard_profile.get("stage1_fraction") or _env_float("CEX_DEX_EXEC_STAGE1_FRACTION", 1.0))
        if 0.0 < stage1_frac < 1.0 and size_micro > self._trade_min_micro():
            staged_size = max(self._trade_min_micro(), int(size_micro * stage1_frac))
            if staged_size < size_micro:
                size_micro = int(staged_size)
                opp["size_usdc"] = int(staged_size)
                opp["size_usdc_micro"] = int(staged_size)
                opp["_staged_original_size_usdc_micro"] = int(original_size_micro)
                logger.info(
                    "EXEC_STAGE1 | pair=%s staged_usdc=%.2f original_usdc=%.2f fraction=%.2f",
                    str(opp.get("pair_label") or "SOL/USDC"),
                    staged_size / 1_000_000.0,
                    original_size_micro / 1_000_000.0,
                    stage1_frac,
                )
        if not self.risk.can_trade(size_micro):
            return False
        is_negative_tier = bool(opp.get("rescue_negative_sim") is True)
        if is_negative_tier:
            budget_ok, budget_reason = self._negative_tier_budget_allows()
            if not budget_ok:
                logger.warning(
                    "BLOCKED: negative-sim tier budget | reason=%s pair=%s",
                    budget_reason,
                    str(opp.get("pair_label") or "SOL/USDC"),
                )
                record_rescue_negative_event(
                    pair=str(opp.get("pair_label") or "SOL/USDC"),
                    outcome="blocked",
                    sim_net_bps=float(opp.get("net_bps") or 0.0),
                    edge_bps=float(opp.get("edge_bps") or opp.get("gross_bps") or 0.0),
                    size_usdc=float(size_micro / 1_000_000.0),
                    blocked_by=f"budget:{budget_reason}",
                )
                return False
            self._negative_tier_record_attempt()

        gross_bps = float(opp.get("gross_bps") or opp.get("edge_bps") or 0)
        net_bps = float(opp.get("net_bps") or 0)
        trade_id = str(opp.get("trade_id") or f"cex-{int(time.time())}")
        base_mint = str(opp.get("base_mint") or SOL_MINT)
        bp_symbol = str(opp.get("backpack_symbol") or "SOL_USDC")
        pair_label = str(opp.get("pair_label") or "SOL/USDC")
        base_symbol = str(opp.get("symbol") or pair_label.split("/")[0]).strip().upper()
        if (
            not self.settings.test_mode
            and not self.settings.simulate
            and _env_bool("CEX_DEX_EXEC_ROUNDTRIP_COOLDOWN_ENABLED", True)
        ):
            cooling, cooling_reason = self._pair_roundtrip_cooling(pair_label)
            if cooling:
                logger.warning("BLOCKED: pair cooldown | %s %s", pair_label, cooling_reason)
                self._log_blocked_attempt(opp, f"pair_cooldown:{cooling_reason}")
                record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
                return False

        if self.settings.test_mode or self.settings.simulate:
            logger.info(
                "[SIMULATE] execute_trade size=$%.2f net=%dbps (no risk ledger write)",
                size_micro / 1e6,
                opp.get("net_bps"),
            )
            if _env_bool("CEX_DEX_RECORD_SIMULATE_PNL", False):
                profit_usdc = (opp["net_bps"] / 10000.0) * (size_micro / 1_000_000.0)
                self.risk.record_trade_result(profit_usdc, size_micro)
                record_trade_execution("cex_dex", success=True, pnl_usd=profit_usdc)
            return True

            self._pair_quality_record_attempt(pair_label)

        if not self.win_rate_tracker.should_approve(min_win_rate=LIVE_MIN_WIN_RATE):
            logger.info("Win rate below threshold - skipping")
            record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
            return False

        wr_ok, wr_reason = self.win_rate_tracker.should_approve_setup(
            gross_bps,
            net_bps,
            pair=pair_label,
        )
        if not wr_ok:
            logger.warning(
                "BLOCKED: win-rate setup gate | gross=%.1f net=%.1f %s",
                gross_bps,
                net_bps,
                wr_reason,
            )
            record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
            return False

        logger.info(
            "EXECUTING | size=$%.1fk net=%dbps win_rate_gate=%s",
            size_micro / 1e6,
            opp["net_bps"],
            wr_reason,
        )

        stable_ok, stable_px, stable_reason = await self._check_cex_stability(
            bp_symbol,
            drift_cap_override_bps=float(guard_profile.get("cex_stability_drift_cap_bps") or 0.0),
        )
        if not stable_ok:
            logger.warning("BLOCKED: CEX stability | %s reason=%s", pair_label, stable_reason)
            self._log_blocked_attempt(opp, f"cex_stability:{stable_reason}")
            record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
            return False

        _cex_buy, _cex_mid, cex_ask = await self.backpack.get_cex_buy_reference_price(bp_symbol)
        cex_px = float(_cex_buy or stable_px or opp.get("cex_price") or 0.0)
        if cex_px > 0:
            opp["cex_price"] = cex_px
            opp["cex_ask"] = cex_ask
            gross_live = float(bps_diff(cex_px, float(opp.get("jup_price") or 0)))
            opp["gross_bps"] = int(round(gross_live))
            opp["edge_bps"] = gross_live
            opp["net_bps"] = int(
                round(
                    net_spread_bps_after_costs(
                        gross_live, size_micro, direction="cex_cheap"
                    )
                )
            )
        required_lamports = self._estimate_sell_lamports(
            size_micro,
            cex_px,
            base_decimals=int(opp.get("base_decimals") or 9),
        )
        opp["size_lamports"] = required_lamports
        inventory_buffer = float(os.getenv("CEX_DEX_INVENTORY_BUFFER_FRAC", "1.05"))
        wallet_sol = await self.get_wallet_sol_balance()
        wallet_lamports = int(wallet_sol * 1_000_000_000)
        use_inventory = (
            _env_bool("CEX_DEX_INVENTORY_FIRST", True)
            and wallet_lamports >= int(required_lamports * inventory_buffer)
        )

        if (
            not self.settings.test_mode
            and not self.settings.simulate
            and self.settings.live_trading_confirm_enabled
        ):
            try:
                if use_inventory:
                    min_sol = float(os.getenv("CEX_DEX_MIN_LEDGER_SOL", "0.12"))
                    if wallet_sol < min_sol:
                        raise InsufficientBalance(
                            f"Wallet SOL for fees: have {wallet_sol:.4f}, need {min_sol:.4f}",
                            asset="SOL",
                        )
                else:
                    await preflight_check(size_micro, backpack=self.backpack)
            except InsufficientBalance as exc:
                logger.warning("BLOCKED: capital preflight | %s", exc)
                self._log_blocked_attempt(opp, f"preflight:{exc.asset or 'capital'}")
                record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
                return False

        run_roundtrip_sim = _env_bool("CEX_DEX_AGGRESSIVE_ROUNDTRIP_SIM", True)
        if _env_bool("GO_LIVE_SMALL_ACCOUNT", False) and use_inventory:
            run_roundtrip_sim = _env_bool("CEX_DEX_ROUNDTRIP_SIM_INVENTORY", False)
        if run_roundtrip_sim:
            roundtrip = RoundtripSimulator(self.jupiter, settings=self.settings)
            rescue_sim = (
                float(opp.get("rescue_sim_net_bps"))
                if opp.get("rescue_sim_net_bps") is not None
                else None
            )
            rescue_ts = int(opp.get("detect_ts_ms") or 0)
            rescue_exec_ms = float(max(0, (int(time.time() * 1000) - rescue_ts))) if rescue_ts > 0 else None
            rescue_quote_return = (
                float(opp.get("rescue_quote_return_bps"))
                if opp.get("rescue_quote_return_bps") is not None
                else None
            )
            rescue_cex_eff = (
                float(opp.get("rescue_cex_effective_buy_price"))
                if opp.get("rescue_cex_effective_buy_price") is not None
                else None
            )
            detect_hops = int(opp.get("detect_route_hops") or 0)
            detect_impact = (
                float(opp.get("detect_price_impact_bps"))
                if opp.get("detect_price_impact_bps") is not None
                else None
            )

            candidate_sizes = self._exec_size_ladder(base_symbol, size_micro)
            if int(size_micro) not in candidate_sizes:
                candidate_sizes = [int(size_micro), *candidate_sizes]
            best_fail: tuple[float, str, dict[str, Any], int] | None = None
            sim_ok = False
            sim_net = float("-inf")
            sim_reason = "no_exec_sim"
            sim_details: dict[str, Any] = {}
            chosen_exec_size = int(size_micro)

            for idx, candidate_size in enumerate(candidate_sizes):
                one_ok, one_net, one_reason, one_details = await roundtrip.run_roundtrip(
                    cex_px,
                    int(candidate_size),
                    backpack_symbol=bp_symbol,
                    base_mint=str(opp.get("base_mint") or SOL_MINT),
                    base_decimals=int(opp.get("base_decimals") or 9),
                    expected_net_bps=float(opp.get("net_bps") or 0),
                )
                if one_ok:
                    sim_ok = True
                    sim_net = float(one_net)
                    sim_reason = str(one_reason)
                    sim_details = dict(one_details)
                    chosen_exec_size = int(candidate_size)
                    break
                if best_fail is None or float(one_net) > float(best_fail[0]):
                    best_fail = (float(one_net), str(one_reason), dict(one_details), int(candidate_size))
                if idx + 1 < len(candidate_sizes):
                    logger.info(
                        "EXEC_ROUNDTRIP_RETRY | pair=%s try_size_usdc=%.2f failed_net=%.2f reason=%s next_retry=%s",
                        pair_label,
                        candidate_size / 1_000_000.0,
                        float(one_net),
                        str(one_reason),
                        idx + 2,
                    )

            if not sim_ok and best_fail is not None:
                sim_net, sim_reason, sim_details, chosen_exec_size = best_fail

            min_net_gate = float(sim_details.get("min_net_bps") or roundtrip_sim_min_net_bps())
            drift_soft_band = _env_float("CEX_DEX_EXEC_ROUNDTRIP_DRIFT_SOFT_BAND_BPS", 0.35)
            drift_max = _env_float("CEX_DEX_EXEC_ROUNDTRIP_DRIFT_MAX_BPS", 12.0)
            if bool(guard_profile.get("tightened")):
                tighten_factor = max(
                    0.25,
                    min(1.0, _env_float("CEX_DEX_RUNTIME_GUARD_TIGHTEN_FACTOR", 0.75)),
                )
                drift_soft_band *= tighten_factor
                drift_max *= tighten_factor
            soft_pass = False
            if not sim_ok and rescue_sim is not None:
                drift = float(rescue_sim) - float(sim_net)
                if (
                    rescue_sim >= min_net_gate
                    and float(sim_net) >= (min_net_gate - drift_soft_band)
                    and drift <= drift_max
                ):
                    soft_pass = True
                    sim_ok = True
                    sim_reason = f"exec_soft_pass_drift_{drift:.2f}"

            if chosen_exec_size != int(size_micro):
                logger.info(
                    "EXEC_SIZE_DOWNSHIFT | pair=%s from_usdc=%.2f to_usdc=%.2f sim_net=%.2f reason=%s",
                    pair_label,
                    size_micro / 1_000_000.0,
                    chosen_exec_size / 1_000_000.0,
                    float(sim_net),
                    sim_reason,
                )
                size_micro = int(chosen_exec_size)
                opp["size_usdc"] = int(chosen_exec_size)
                opp["size_usdc_micro"] = int(chosen_exec_size)

            if rescue_sim is not None:
                record_roundtrip_divergence_event(
                    pair=pair_label,
                    outcome="soft_pass" if soft_pass else ("pass" if sim_ok else "blocked"),
                    reason=sim_reason,
                    rescue_sim_bps=float(rescue_sim),
                    exec_sim_bps=float(sim_net),
                    rescue_to_exec_ms=rescue_exec_ms,
                )

            exec_quote_return = None
            in_micro = int(sim_details.get("usdc_in_micro") or 0)
            out_micro = int(sim_details.get("usdc_back_micro") or 0)
            if in_micro > 0 and out_micro > 0:
                exec_quote_return = ((out_micro - in_micro) / in_micro) * 10_000.0
            cex_exec_eff = (
                float(sim_details.get("cex_effective_buy_price"))
                if sim_details.get("cex_effective_buy_price") is not None
                else None
            )
            cex_drift_bps = None
            if rescue_cex_eff and cex_exec_eff and rescue_cex_eff > 0:
                cex_drift_bps = ((cex_exec_eff - rescue_cex_eff) / rescue_cex_eff) * 10_000.0
            quote_drift_bps = None
            if rescue_quote_return is not None and exec_quote_return is not None:
                quote_drift_bps = float(exec_quote_return) - float(rescue_quote_return)

            exec_hops = int(sim_details.get("route_hops") or 0)
            exec_impact = (
                float(sim_details.get("sell_price_impact_bps"))
                if sim_details.get("sell_price_impact_bps") is not None
                else None
            )
            hops_delta = abs(exec_hops - detect_hops) if detect_hops > 0 else 0
            impact_delta = (
                abs(float(exec_impact) - float(detect_impact))
                if (exec_impact is not None and detect_impact is not None)
                else 0.0
            )
            if _env_bool("CEX_DEX_EXEC_ROUTE_DRIFT_GUARD_ENABLED", True):
                max_hops_delta = max(1, _env_int("CEX_DEX_EXEC_ROUTE_HOPS_MAX_DELTA", 2))
                max_impact_delta = max(5.0, _env_float("CEX_DEX_EXEC_ROUTE_IMPACT_MAX_DELTA_BPS", 120.0))
                if hops_delta > max_hops_delta and impact_delta > max_impact_delta:
                    sim_ok = False
                    sim_reason = f"route_drift_h{hops_delta}_i{impact_delta:.1f}"

            if not sim_ok:
                self._pair_roundtrip_record_fail(pair_label, float(sim_net))
                self._pair_quality_record_roundtrip_block(
                    pair_label,
                    rescue_sim_bps=rescue_sim,
                    exec_sim_bps=float(sim_net),
                )
                drift_abs = 0.0
                if rescue_sim is not None:
                    drift_abs = abs(float(rescue_sim) - float(sim_net))
                self._runtime_quality_record(
                    roundtrip_blocked=True,
                    drift_abs_bps=drift_abs,
                    filled=False,
                )
                logger.warning(
                    "BLOCKED: roundtrip pre-sim | net=%.1fbps reason=%s hops=%s rescue_sim=%s delta=%s cex_drift=%s quote_drift=%s route_hops_delta=%s impact_delta=%s rescue_to_exec_ms=%s",
                    float(sim_net),
                    sim_reason,
                    sim_details.get("route_hops"),
                    "n/a" if rescue_sim is None else f"{float(rescue_sim):.2f}",
                    (
                        "n/a"
                        if rescue_sim is None
                        else f"{(float(rescue_sim) - float(sim_net)):.2f}"
                    ),
                    "n/a" if cex_drift_bps is None else f"{cex_drift_bps:.2f}",
                    "n/a" if quote_drift_bps is None else f"{quote_drift_bps:.2f}",
                    hops_delta,
                    f"{impact_delta:.2f}" if impact_delta else "n/a",
                    "n/a" if rescue_exec_ms is None else f"{rescue_exec_ms:.0f}",
                )
                self._log_blocked_attempt(opp, f"roundtrip_sim:{sim_reason}")
                record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
                return False

            self._pair_roundtrip_record_success(pair_label, float(sim_net))
            self._pair_route_pref_update(pair_label, int(sim_details.get("route_hops") or 0))

            if _env_bool("CEX_DEX_JUPITER_ROUNDTRIP_CHECK", False):
                jup_ok, jup_net, jup_reason = await pre_simulate_full_jupiter_roundtrip(
                    self.jupiter,
                    size_micro,
                )
                if not jup_ok:
                    logger.warning(
                        "BLOCKED: Jupiter roundtrip pre-sim | net=%.1fbps %s",
                        jup_net,
                        jup_reason,
                    )
                    record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
                    return False

        required_lamports = self._estimate_sell_lamports(
            size_micro,
            cex_px,
            base_decimals=int(opp.get("base_decimals") or 9),
        )
        opp["size_lamports"] = required_lamports
        wallet_sol = await self.get_wallet_sol_balance()
        wallet_lamports = int(wallet_sol * 1_000_000_000)
        use_inventory = (
            _env_bool("CEX_DEX_INVENTORY_FIRST", True)
            and wallet_lamports >= int(required_lamports * inventory_buffer)
        )

        from src.core.onchain_profit import assert_roundtrip_profit, fetch_usdc_balance_micro

        usdc_before = await fetch_usdc_balance_micro()

        wallet = self._wallet_pubkey()
        if not wallet:
            logger.error("WALLET_PUBKEY not configured")
            return False

        if not await self.jupiter.has_signing():
            logger.error(
                "BLOCKED: no signing backend — set SIGNER_TYPE=hot and PRIVATE_KEY_FILE, "
                "or allow hot key in non-prod"
            )
            self._log_blocked_attempt(opp, "signing_unavailable")
            record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
            return False

        if base_symbol != "SOL" and not _env_bool("CEX_DEX_ENABLE_MIDCAP_LIVE", True):
            logger.warning("BLOCKED: midcap live disabled by env | %s", pair_label)
            self._log_blocked_attempt(opp, "midcap_live_disabled")
            record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
            return False

        inv_before = await self._inventory_snapshot()

        if use_inventory:
            logger.info("Using on-chain inventory (fast path)")
            swap_ok = await self._execute_jupiter_sell_only(
                opp,
                size_micro=size_micro,
                cex_px=cex_px,
                wallet=wallet,
            )
        else:
            logger.info("Full CEX buy + withdraw path")
            if not await self._check_cex_balance_before_buy(size_micro):
                self._log_blocked_attempt(opp, "insufficient_backpack_usdc")
                record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
                return False

            depth_ok = await self.backpack.check_ask_depth(
                symbol=base_symbol,
                required_usdc=size_micro,
            )
            if not depth_ok:
                depth_reason = "insufficient_depth"
                logger.warning(
                    "BLOCKED: thin Backpack book | size=$%.2f reason=%s",
                    size_micro / 1e6,
                    depth_reason,
                )
                self._log_blocked_attempt(opp, f"depth:{depth_reason}")
                record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
                return False

            swap_ok = await self._execute_full_cex_dex_sol(
                opp,
                size_micro=size_micro,
                cex_px=cex_px,
                wallet=wallet,
            )

        inv_after = await self._inventory_snapshot()
        self._log_inventory_shift(
            pair_label,
            before=inv_before,
            after=inv_after,
            path=str(opp.get("_execution_path") or ("inventory" if use_inventory else "cex_withdraw")),
        )

        profit_usdc = (opp["net_bps"] / 10000.0) * (size_micro / 1_000_000.0)
        slippage_logged = float(opp.get("_last_exec_slippage_bps") or 0.0)
        if swap_ok:
            if usdc_before is not None:
                usdc_after = await fetch_usdc_balance_micro()
                if usdc_after is not None:
                    ok_profit, details = await assert_roundtrip_profit(
                        usdc_before_micro=usdc_before,
                        usdc_after_micro=usdc_after,
                        trade_size_micro=size_micro,
                        expected_net_bps=float(opp.get("net_bps") or 0),
                        settings=self.settings,
                    )
                    if not ok_profit:
                        from src.core.circuit_breaker import circuit_breaker

                        circuit_breaker.trip("onchain_profit_assert_failed")
                        record_trade_execution(
                            "cex_dex",
                            success=False,
                            pnl_usd=0.0,
                            slippage_bps=slippage_logged,
                        )
                        self._record_win_rate_outcome(
                            opp, success=False, realized_usdc=0.0, trade_id=trade_id
                        )
                        return False
                    realized_bps = float(details.get("realized_bps") or 0)
                    delta_micro = int(details.get("delta_micro") or 0)
                    realized_usdc = delta_micro / 1_000_000.0
                    profit_usdc = realized_usdc
                    modeled = float(resolve_slippage_bps(base_mint, USDC_MINT))
                    slip_est = max(0.0, modeled - realized_bps) if realized_bps > 0 else modeled
                    slippage_logged = slip_est
            self.risk.record_trade_result(profit_usdc, size_micro)
            record_trade_execution(
                "cex_dex",
                success=True,
                pnl_usd=profit_usdc,
                slippage_bps=slippage_logged,
            )
            self._pair_quality_record_fill(pair_label)
            self._runtime_quality_record(
                roundtrip_blocked=False,
                drift_abs_bps=0.0,
                filled=True,
            )
            self._record_win_rate_outcome(
                opp, success=True, realized_usdc=profit_usdc, trade_id=trade_id
            )
            if slippage_logged > 0:
                record_execution_slippage("cex_dex", slippage_logged)
            if is_negative_tier:
                self._negative_tier_record_outcome(True)
            return True

        # Use 0 penalty when the CEX order was rejected before any fill (no funds moved).
        # For real mid-execution failures use a configurable penalty (default 5 USD).
        pre_fill_reject = bool(opp.get("_cex_pre_fill_reject"))
        fail_penalty = 0.0 if pre_fill_reject else -float(
            os.getenv("CEX_DEX_EXEC_FAIL_PENALTY_USDC", "5.0")
        )
        self.risk.record_trade_result(fail_penalty, size_micro)
        record_trade_execution(
            "cex_dex",
            success=False,
            pnl_usd=fail_penalty,
            slippage_bps=slippage_logged,
        )
        self._record_win_rate_outcome(
            opp, success=False, realized_usdc=fail_penalty, trade_id=trade_id
        )
        if is_negative_tier:
            self._negative_tier_record_outcome(False)
        return False

    async def _execute_jupiter_sell_with_retries(
        self,
        size_micro: int,
        cex_price: float,
        wallet: str,
        opp: dict[str, Any],
    ) -> bool:
        """Fresh base→USDC quote + swap build + Jito bundle with retries."""
        max_attempts = int(os.getenv("CEX_DEX_EXEC_SWAP_MAX_ATTEMPTS", "3"))
        base_mint = str(opp.get("base_mint") or SOL_MINT)
        base_decimals = int(opp.get("base_decimals") or 9)
        slippage = resolve_slippage_bps(base_mint, USDC_MINT)
        net_bps_f = float(opp.get("net_bps") or 0)
        gross_bps_f = float(opp.get("gross_bps") or opp.get("edge_bps") or 0)
        ai_confidence = float(opp.get("ai_confidence") or opp.get("confidence") or 75)
        modeled_net_usd_val = float(opp.get("net_profit_usd") or 0) or modeled_net_usd(
            net_bps_f, size_micro
        )
        if modeled_net_usd_val <= 0:
            modeled_net_usd_val = float(os.getenv("JITO_TIP_FALLBACK_NET_USD", "8.0"))
        if mev_protection_enabled():
            jito_tip = calculate_optimal_tip(
                modeled_net_usd_val,
                gross_bps_f,
                confidence=ai_confidence,
                tip_floor=get_cached_tip_floor(),
            )
            log_jito_tip(jito_tip, modeled_net_usd_val)
        else:
            jito_tip = int(os.getenv("JITO_TIP_LAMPORTS", "100000"))
        usdc = size_micro / 1_000_000.0
        cex_fee_fudge = float(os.getenv("CEX_DEX_CEX_BUY_FILL_FUDGE", "0.995"))

        chain_lamports = int(opp.get("_chain_sell_lamports") or 0)

        for attempt in range(max_attempts):
            if chain_lamports > 0:
                base_raw = chain_lamports
            else:
                base_raw = int(
                    (usdc / max(cex_price, 1e-9))
                    * (10**base_decimals)
                    * cex_fee_fudge
                )
                base_raw = max(base_raw, 1)

            sell_quote = await self.jupiter.fetch_quote_raw(
                base_raw,
                input_mint=base_mint,
                output_mint=USDC_MINT,
                slippage_bps=slippage,
            )
            if not sell_quote:
                logger.warning(
                    "Jupiter sell quote failed (attempt %s/%s)",
                    attempt + 1,
                    max_attempts,
                )
                await asyncio.sleep(0.6 * (attempt + 1))
                continue

            hops = quote_route_hops(sell_quote)
            logger.info(
                "Jupiter sell quote | attempt=%s hops=%s slippage=%sbps",
                attempt + 1,
                hops,
                slippage,
            )

            swap_data = await self.jupiter.build_swap_transaction(
                {"quote": sell_quote},
                wallet,
                slippage_bps=slippage,
            )
            if not swap_data or "swapTransaction" not in swap_data:
                logger.warning(
                    "Jupiter swap build failed (attempt %s/%s)",
                    attempt + 1,
                    max_attempts,
                )
                await asyncio.sleep(0.6 * (attempt + 1))
                continue

            tx_b64 = swap_data["swapTransaction"]
            signed_b64 = await self.jupiter.sign_swap_transaction_b64(tx_b64)
            if not signed_b64:
                logger.warning(
                    "Jupiter swap sign failed (attempt %s/%s) — check PRIVATE_KEY_FILE / keypair",
                    attempt + 1,
                    max_attempts,
                )
                await asyncio.sleep(0.6 * (attempt + 1))
                continue

            bundle_id = ""
            result = await send_bundle_multi_region(signed_b64, tip_lamports=jito_tip)
            if multi_region_enabled():
                logger.info(
                    "Multi-region send: %s/%s regions accepted",
                    result.get("success_count", 0),
                    result.get("total_regions", 0),
                )
            success = bool(result.get("success"))
            if success and result.get("bundle_id"):
                bundle_id = str(result["bundle_id"])
                if _env_bool("JITO_AWAIT_BUNDLE_POLL", True):
                    success = await self.jito.await_bundle_landed(bundle_id)
            else:
                bundle_id = str(result.get("bundle_id") or "")

            if success:
                if bundle_id:
                    opp["tx_sig"] = bundle_id
                try:
                    out_usdc = int(sell_quote.get("outAmount", 0))
                    expected_usdc = int(size_micro * (1 + float(opp.get("net_bps", 0)) / 10000.0))
                    if out_usdc > 0 and expected_usdc > 0:
                        slip_bps = max(
                            0.0,
                            (1.0 - out_usdc / expected_usdc) * 10_000.0,
                        )
                        opp["_last_exec_slippage_bps"] = slip_bps
                except (TypeError, ValueError):
                    pass
                logger.info(
                    "Jupiter sell landed | size=$%.2f net_est=%dbps",
                    size_micro / 1e6,
                    opp.get("net_bps"),
                )
                return True

            logger.warning(
                "Jupiter/Jito sell attempt %s/%s failed",
                attempt + 1,
                max_attempts,
            )
            await asyncio.sleep(0.8 * (attempt + 1))

        return False

    async def close(self) -> None:
        await self.backpack.close()
        await self.jupiter.close()
        await self.jito.close()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


_cex_dex_strategy_singleton: CexDexStrategy | None = None


def get_cex_dex_strategy() -> CexDexStrategy:
    """Lazy module singleton (avoids import-time client init)."""
    global _cex_dex_strategy_singleton
    if _cex_dex_strategy_singleton is None:
        _cex_dex_strategy_singleton = CexDexStrategy()
    return _cex_dex_strategy_singleton
