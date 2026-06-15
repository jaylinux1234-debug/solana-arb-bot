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


def _load_proven_midcaps() -> frozenset[str]:
    raw = (os.getenv("CEX_PROVEN_MIDCAPS") or "").strip()
    if raw:
        return frozenset(s.strip().upper() for s in raw.split(",") if s.strip())
    return frozenset(PROVEN_MIDCAPS)


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

    async def _quick_gross_bps(self) -> float:
        """Lightweight gross spread probe for vol gate (primary pair only)."""
        pair = self._pairs[0] if self._pairs else None
        if pair is None:
            return 0.0
        cex_buy, _, _ = await self.backpack.get_cex_buy_reference_price(pair.backpack_symbol)
        if not cex_buy or cex_buy <= 0:
            return 0.0
        jup_price = await self._probe_jupiter_sell_price(pair, float(cex_buy))
        if not jup_price or jup_price <= 0:
            return 0.0
        from src.utils.price import bps_diff

        return abs(float(bps_diff(cex_buy, jup_price)))

    async def run_cycle(self) -> bool:
        """Main strategy cycle — CEX-DEX scan, smart path execute, then reverse fallback."""
        if not self.risk.can_trade(0):
            return False

        from src.strategies.volatility_gate import should_skip_low_vol_cycle

        gates = await self.vol_gate.get_adaptive_gates()
        vol_5m = await self.vol_gate.get_5min_volatility()
        current_gross = await self._quick_gross_bps()
        if should_skip_low_vol_cycle(vol_5m, current_gross):
            logger.debug(
                "Cycle skipped (low vol) | vol_5m=%s gross_bps=%.2f",
                vol_5m,
                current_gross,
            )
            return False

        logger.info(
            "Adaptive gates: %s | vol=%.2f%%",
            gates.get("mode"),
            vol_5m,
        )

        opportunity = await self._scan_cex_dex_opportunity(gates)
        if opportunity:
            result = await self._execute_smart_path(opportunity)
            logged = await self._log_trade(result)
            return bool(
                logged.get("live_fill")
                or logged.get("success")
                or result.get("success")
            )

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

    async def _scan_cex_dex_opportunity(self, gates: dict[str, Any]) -> dict[str, Any] | None:
        """Scan SOL/USDC with adaptive gates; returns execution-ready opportunity dict."""
        pair = next((p for p in self._pairs if p.symbol.upper() == "SOL"), None)
        if pair is None and self._pairs:
            pair = self._pairs[0]
        if pair is None:
            return None

        _, _, cex_ask = await self.backpack.get_cex_buy_reference_price(
            pair.backpack_symbol
        )

        opp = await self._detect_pair_opportunity(pair, gates=gates)
        if opp is None:
            return None

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
                pair=pair.pair_label,
            )
            return None

        opp["type"] = "cex_dex"
        opp["ai_conf"] = ai_conf
        opp["path"] = "smart"
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

    async def detect_opportunity(self) -> dict[str, Any] | None:
        """Scan all configured pairs; return best ``cex_cheap`` opportunity by net bps."""
        gates = await self.get_dynamic_gates()
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

        best: dict[str, Any] | None = None
        for pair in self._pairs:
            if not await self.should_trade_pair(pair):
                continue
            try:
                opp = await self._detect_pair_opportunity(pair, gates=gates)
            except Exception as exc:
                logger.debug("CEX-DEX scan %s failed: %s", pair.symbol, exc)
                continue
            if opp is None:
                continue
            if best is None or float(opp["net_bps"]) > float(best["net_bps"]):
                best = opp
        if best:
            logger.info(
                "OPPORTUNITY | pair=%s edge=%.1fbps net=%.1fbps size=$%.2f conf=%.1f%%",
                best.get("pair_label"),
                best.get("edge_bps"),
                best.get("net_bps"),
                int(best["size_usdc"]) / 1e6,
                best.get("confidence"),
            )
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

            record_cex_price(float(cex_buy))
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

        size_usdc = await self._calculate_size(int(gross_bps), cex_buy)
        size_usdc = self._clamp_opportunity_size(size_usdc)
        # Recompute edge/net at execution size using buy (ask) price.
        edge_bps = float(bps_diff(cex_buy, jup_price))
        gross_bps = edge_bps
        net_bps = net_spread_bps_after_costs(
            gross_bps,
            size_usdc,
            direction="cex_cheap",
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
            "CEX-DEX Scan | pair=%s edge_bps=%.1f spread_abs=%.1f cost_bps=%.1f net_bps=%.1f "
            "dir=%s confidence=%.1f size_usdc=%d probe=%d",
            pair.pair_label,
            edge_bps,
            spread_abs,
            cost_bps,
            net_bps,
            spread.direction,
            confidence,
            size_usdc,
            probe_micro,
        )

        if not self._is_sane_opportunity(edge_bps, net_bps, size_usdc):
            self.log_near_miss(
                edge_bps, net_bps, confidence, "sanity_reject", pair=pair.pair_label
            )
            return None

        if not self._is_profitable_opportunity(edge_bps, net_bps, confidence, gates=gates):
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
                        edge_bps, net_bps, confidence, reason, pair=pair.pair_label
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
            "gate_mode": gates.get("mode", "strict"),
            "dynamic_gates": gates,
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

    def _max_trade_usdc_micro(self) -> int:
        return int(
            os.getenv(
                "CEX_DEX_MAX_TRADE_USDC_MICRO",
                str(int(getattr(self.settings, "CEX_DEX_MAX_TRADE_USDC_MICRO", 25_000_000))),
            )
        )

    def _clamp_opportunity_size(self, size_micro: int) -> int:
        min_micro = int(self.settings.trading.min_flash_usdc * 1_000_000)
        return clamp_trade_usdc_micro(
            max_trade_usdc_micro=self._max_trade_usdc_micro(),
            flash_cap_usdc_micro=int(self.settings.trading.max_flash_usdc * 1_000_000),
            liquidity_cap_usdc_micro=size_micro,
            min_trade_usdc_micro=min_micro,
        )

    def _is_sane_opportunity(
        self,
        edge_bps: float,
        net_bps: float,
        size_micro: int,
    ) -> bool:
        """Reject fantasy spreads/sizes from bad quotes or stale config."""
        max_net = float(os.getenv("CEX_DEX_MAX_MODELED_NET_BPS", "80"))
        max_gross = float(os.getenv("CEX_DEX_MAX_MODELED_GROSS_BPS", "120"))
        if edge_bps > max_gross or net_bps > max_net:
            return False
        if size_micro <= 0 or size_micro > self._max_trade_usdc_micro():
            return False
        return True

    async def _calculate_size(self, gross_bps: int, cex_price: float) -> int:
        """Dynamic position sizing (USDC micro-units), scaled by gross edge not modeled net."""
        _ = cex_price
        base_size = int(
            self.settings.trading.max_flash_usdc
            * self.settings.trading.flash_size_utilization
            * 1_000_000
        )
        edge_factor = min(1.2, max(0.5, float(gross_bps) / 25.0))
        size = int(base_size * edge_factor)
        min_micro = int(self.settings.trading.min_flash_usdc * 1_000_000)
        max_micro = int(self.settings.trading.max_flash_usdc * 1_000_000)
        size = max(min_micro, min(max_micro, size))

        if self.settings.trading.dynamic_amount:
            safe_cap = await self.get_safe_trade_size()
            if safe_cap > 0:
                size = min(size, safe_cap)
            else:
                size = 0
        return size

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
        logger.info(
            "FULL CEX-DEX SOL | buying $%.2f USDC on Backpack",
            size_micro / 1e6,
        )

        sol_before = await get_ledger_sol_balance()
        withdraw_ref = await self.backpack.execute_cex_buy_then_withdraw(
            size_micro,
            pair=bp_symbol,
            destination=wallet,
        )
        if not withdraw_ref:
            logger.error("CEX buy or SOL withdraw failed")
            return False

        logger.info("SOL withdrawn to wallet | ref=%s", withdraw_ref)

        sol_after = await get_ledger_sol_balance()
        reserve_sol = float(os.getenv("CEX_DEX_SOL_SELL_RESERVE_SOL", "0.02"))
        delta_sol = max(0.0, sol_after - sol_before)
        sell_sol = max(0.0, delta_sol - reserve_sol)
        if sell_sol <= 0:
            fudge = float(os.getenv("CEX_DEX_CEX_BUY_FILL_FUDGE", "0.995"))
            sell_sol = (size_micro / 1e6) / max(cex_px, 1e-9) * fudge
            sell_sol = min(sell_sol, max(0.0, sol_after - reserve_sol))

        base_raw = int(sell_sol * 1_000_000_000)
        if base_raw < 1:
            logger.error(
                "No on-chain SOL to sell after withdraw | before=%.4f after=%.4f",
                sol_before,
                sol_after,
            )
            return False

        opp["_chain_sell_lamports"] = base_raw
        opp["_execution_path"] = "cex_withdraw"
        logger.info(
            "Jupiter sell sizing | lamports=%s (~%.4f SOL)",
            base_raw,
            sell_sol,
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
        if not self.risk.can_trade(size_micro):
            return False

        gross_bps = float(opp.get("gross_bps") or opp.get("edge_bps") or 0)
        net_bps = float(opp.get("net_bps") or 0)
        trade_id = str(opp.get("trade_id") or f"cex-{int(time.time())}")
        base_mint = str(opp.get("base_mint") or SOL_MINT)
        bp_symbol = str(opp.get("backpack_symbol") or "SOL_USDC")
        pair_label = str(opp.get("pair_label") or "SOL/USDC")
        base_symbol = str(opp.get("symbol") or pair_label.split("/")[0]).strip().upper()

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

        cex_buy, _cex_mid, cex_ask = await self.backpack.get_cex_buy_reference_price(
            bp_symbol
        )
        cex_px = float(cex_buy or opp.get("cex_price") or 0.0)
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
            sim_ok, sim_net, sim_reason, sim_details = await roundtrip.run_roundtrip(
                cex_px,
                size_micro,
                base_mint=str(opp.get("base_mint") or SOL_MINT),
                base_decimals=int(opp.get("base_decimals") or 9),
                expected_net_bps=float(opp.get("net_bps") or 0),
            )
            if not sim_ok:
                logger.warning(
                    "BLOCKED: roundtrip pre-sim | net=%.1fbps reason=%s hops=%s",
                    sim_net,
                    sim_reason,
                    sim_details.get("route_hops"),
                )
                self._log_blocked_attempt(opp, f"roundtrip_sim:{sim_reason}")
                record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
                return False

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

        if base_symbol != "SOL":
            logger.warning("BLOCKED: midcap unsupported for live CEX→withdraw→DEX | %s", pair_label)
            self._log_blocked_attempt(opp, "midcap_unsupported")
            record_trade_execution("cex_dex", success=False, pnl_usd=0.0)
            return False

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
            self._record_win_rate_outcome(
                opp, success=True, realized_usdc=profit_usdc, trade_id=trade_id
            )
            if slippage_logged > 0:
                record_execution_slippage("cex_dex", slippage_logged)
            return True

        self.risk.record_trade_result(-50.0, size_micro)
        record_trade_execution(
            "cex_dex",
            success=False,
            pnl_usd=-50.0,
            slippage_bps=slippage_logged,
        )
        self._record_win_rate_outcome(
            opp, success=False, realized_usdc=-50.0, trade_id=trade_id
        )
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


_cex_dex_strategy_singleton: CexDexStrategy | None = None


def get_cex_dex_strategy() -> CexDexStrategy:
    """Lazy module singleton (avoids import-time client init)."""
    global _cex_dex_strategy_singleton
    if _cex_dex_strategy_singleton is None:
        _cex_dex_strategy_singleton = CexDexStrategy()
    return _cex_dex_strategy_singleton
