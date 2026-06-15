"""Multi-strategy brain: volatility gates, lane scoring, and strategy selection."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from src.config.settings import Settings, get_settings
from src.strategies.volatility_gate import VolatilityGate

logger = logging.getLogger(__name__)


@dataclass
class BrainOpportunity:
    """Minimal lane descriptor for priority scoring."""

    strategy: str
    base_score: float
    gross_bps: float = 0.0
    net_bps: float = 0.0
    active: bool = False


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def cex_dex_brain_priority_bias(settings: Settings | None = None) -> float:
    cfg = settings or get_settings()
    raw = os.getenv("CEX_DEX_BRAIN_PRIORITY_BIAS", "").strip()
    if raw:
        return float(raw)
    return float(getattr(cfg, "CEX_DEX_BRAIN_PRIORITY_BIAS", 80))


def calculate_priority(opp: BrainOpportunity, *, settings: Settings | None = None) -> float:
    """
    Dynamic CEX-DEX bias: base score + configured bias + 3 pts per gross bps when edge >= 8.
    """
    if opp.strategy == "cex_dex" and opp.gross_bps >= 8:
        bias = cex_dex_brain_priority_bias(settings) + (opp.gross_bps * 3)
        return opp.base_score + bias
    return opp.base_score


def cex_dex_dynamic_bias_from_snapshot(snapshot: dict | None) -> float:
    """Bias increment for ``cex_dex`` lane from brain snapshot gross bps."""
    from src.strategies.brain_signals import cex_dex_gross_bps_from_snapshot

    gross = cex_dex_gross_bps_from_snapshot(snapshot)
    if gross is None or gross < 8:
        return 0.0
    return cex_dex_brain_priority_bias() + (gross * 3)


def apply_dynamic_cex_dex_score(
    base_score: float,
    snapshot: dict | None,
) -> float:
    """Return ``base_score`` with dynamic CEX-DEX bias when snapshot gross >= 8 bps."""
    bonus = cex_dex_dynamic_bias_from_snapshot(snapshot)
    if bonus <= 0:
        return base_score
    return base_score + bonus


class StrategyBrain:
    """
    Gate-aware strategy selector: ``VolatilityGate`` tiers + ``DexCexReverseStrategy`` lane.
    """

    def __init__(
        self,
        backpack_client: Any,
        jupiter_executor: Any | None = None,
        *,
        settings: Settings | None = None,
        wallet_pubkey: str | None = None,
        risk: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.backpack = backpack_client
        self.jupiter = jupiter_executor
        self.vol_gate = VolatilityGate(backpack_client, jupiter_executor)
        self._wallet_pubkey = wallet_pubkey
        self._risk = risk
        self._reverse_strategy: Any | None = None

    @property
    def reverse_strategy(self) -> Any:
        """Lazy DEX→CEX lane (execution uses ``multi_strategy_cycle``)."""
        if self._reverse_strategy is None:
            from src.strategies.dex_cex_reverse import DexCexReverseStrategy

            self._reverse_strategy = DexCexReverseStrategy(
                jupiter_executor=self.jupiter,
                backpack_client=self.backpack,
                wallet_pubkey=self._wallet_pubkey
                or self.settings.wallet_pubkey
                or getattr(self.settings, "WALLET_PUBKEY", None),
                settings=self.settings,
                risk=self._risk,
            )
        return self._reverse_strategy

    def _lane_metrics(self, lane: str, snapshot: dict) -> tuple[float, float, bool]:
        """Return (gross_bps, net_bps, active) for a strategy lane."""
        from src.strategies.brain_signals import (
            backrun_signal_present,
            cex_dex_gross_bps_from_snapshot,
            dex_cex_reverse_signal_present,
            lane_signal_present,
            liquidation_signal_present,
        )

        snap = snapshot or {}
        if lane == "cex_dex":
            cx = snap.get("cex_dex") or snap.get("cex_dex_best") or {}
            gross = cex_dex_gross_bps_from_snapshot(snapshot) or 0.0
            try:
                net = float(cx.get("spread_bps_net") or cx.get("net_bps") or 0.0)
            except (TypeError, ValueError):
                net = 0.0
            active = bool(cx.get("active"))
            return float(gross), net, active

        if lane == "dex_cex_reverse":
            rev = snap.get("dex_cex_reverse") or {}
            try:
                gross = float(rev.get("gross_bps") or 0.0)
                net = float(rev.get("net_bps") or 0.0)
            except (TypeError, ValueError):
                gross, net = 0.0, 0.0
            active = dex_cex_reverse_signal_present(snapshot)
            return gross, net, active

        if lane == "liquidation":
            liq = snap.get("liquidation_best") or {}
            try:
                profit = float(liq.get("profit_usdc") or 0.0)
            except (TypeError, ValueError):
                profit = 0.0
            active = liquidation_signal_present(snapshot)
            return profit, profit, active

        if lane == "collateral_swap":
            col = snap.get("collateral_best") or {}
            try:
                spread = float(col.get("spread_bps") or 0.0)
            except (TypeError, ValueError):
                spread = 0.0
            active = bool(col.get("active")) or spread >= 150.0
            return spread, spread, active

        if lane == "backrun":
            br = snap.get("backrun") or {}
            try:
                amt = int(br.get("amount_micro") or 0)
            except (TypeError, ValueError):
                amt = 0
            active = backrun_signal_present(snapshot)
            return float(amt / 1_000_000), float(amt / 1_000_000), active

        return 0.0, 0.0, lane_signal_present(snapshot, lane)

    def build_opportunities_from_snapshot(self, snapshot: dict) -> list[dict[str, Any]]:
        """Build scored lane descriptors from the cycle snapshot."""
        from src.utils.ai import _STRATEGY_KEYS, heuristic_strategy_scores

        heuristic = heuristic_strategy_scores(snapshot)
        scores = heuristic.get("scores") or {}
        opportunities: list[dict[str, Any]] = []

        for lane in _STRATEGY_KEYS:
            gross, net, active = self._lane_metrics(lane, snapshot)
            opportunities.append(
                {
                    "strategy": lane,
                    "base_score": float(scores.get(lane, 0) or 0),
                    "gross_bps": gross,
                    "net_bps": net,
                    "active": active,
                }
            )
        return opportunities

    def score_strategy(self, strategy_name: str, signal: dict[str, Any]) -> float:
        """Advanced lane scoring with MEV priority and small-account safety."""
        gross = float(signal.get("gross_bps") or 0.0)
        base_score = float(signal.get("ai_confidence") or signal.get("base_score") or 60.0)
        base_score += gross * 2.5

        if strategy_name == "cex_dex":
            base_score += float(os.getenv("CEX_DEX_BRAIN_PRIORITY_BIAS", 60))

        weak_gross = float(os.getenv("BRAIN_CEX_DEX_WEAK_GROSS_BPS", 15))
        if strategy_name == "cex_dex" and gross < weak_gross:
            base_score -= 35.0

        mev_edge = float(signal.get("mev_edge") or gross)
        if strategy_name in ("backrun", "liquidation", "collateral_swap"):
            base_score += 45.0 if mev_edge > 8.0 else 20.0

        if _env_bool("GO_LIVE_SMALL_ACCOUNT", False):
            base_score = max(base_score * 0.85, base_score - 15.0)

        return base_score

    def _calculate_score(self, opp: dict[str, Any], gates: dict[str, Any]) -> float:
        """Score one lane using adaptive volatility gates."""
        lane = str(opp.get("strategy") or "")
        base = self.score_strategy(lane, opp)
        gross = float(opp.get("gross_bps") or 0.0)
        net = float(opp.get("net_bps") or 0.0)
        active = bool(opp.get("active"))

        if not active and lane != "cex_dex":
            return base * 0.2

        min_gross = float(gates.get("min_gross") or gates.get("gross") or 9.0)
        min_net = float(gates.get("min_net") or gates.get("net") or 3.0)
        mode = str(gates.get("mode") or "strict")

        brain_opp = BrainOpportunity(
            strategy=lane,
            base_score=base,
            gross_bps=gross,
            net_bps=net,
            active=active,
        )
        score = calculate_priority(brain_opp, settings=self.settings)

        if lane in ("cex_dex", "dex_cex_reverse"):
            if not active:
                return score * 0.35
            if gross >= min_gross and net >= min_net:
                score += 12.0 + gross * 1.5 + max(0.0, net)
            elif gross >= min_gross:
                score += 6.0 + gross * 0.5
            else:
                score *= 0.55

        if lane == "dex_cex_reverse" and active:
            reverse_bias = float(os.getenv("DEX_CEX_REVERSE_BRAIN_BIAS", "24"))
            score += reverse_bias
            if mode == "aggressive":
                score += 8.0
            elif mode == "opportunistic":
                score += 4.0

        if lane == "backrun" and active:
            score += float(os.getenv("BACKRUN_BRAIN_BIAS", "12"))

        if lane == "cex_dex" and _env_bool("GO_LIVE_SMALL_ACCOUNT", False):
            score += float(os.getenv("CEX_DEX_GO_LIVE_BRAIN_BIAS", "18"))

        if lane == "liquidation" and active and gross >= 5.0:
            score += min(40.0, gross * 2.0)

        if lane == "collateral_swap" and active and gross >= 150.0:
            score += min(30.0, gross / 10.0)

        return score

    async def select_best_strategy(
        self,
        opportunities: list[dict[str, Any]],
        *,
        snapshot: dict | None = None,
    ) -> dict[str, Any]:
        """Pick the highest-scoring lane after adaptive gate scoring."""
        gates = await self.vol_gate.get_adaptive_gates()

        scored: list[tuple[float, dict[str, Any]]] = []
        for opp in opportunities:
            score = self._calculate_score(opp, gates)
            scored.append((score, opp))

        if not scored:
            return {
                "strategy": "cex_dex",
                "score": 0.0,
                "gates": gates,
                "scores": {},
                "source": "strategy_brain",
            }

        score_map = {str(o["strategy"]): s for s, o in scored}

        if _env_bool("ENABLE_ADAPTIVE_ROUTER", True):
            try:
                from src.strategies.adaptive_router import AdaptiveLaneRouter

                router = AdaptiveLaneRouter(self.backpack)
                vol_pct = float(gates.get("vol_5m") or 0.0)
                score_map = await router.adjust_scores(
                    score_map, vol_5m_pct=vol_pct, snapshot=snapshot
                )
            except Exception as exc:
                logger.warning("AdaptiveRouter failed: %s", exc)

        if _env_bool("ENABLE_DIRECTION_AWARE_BRAIN", True):
            from src.strategies.brain_router import BrainRouter

            brain_router = BrainRouter(settings=self.settings)
            picked, adjusted = await brain_router.pick_primary_lane(
                score_map, snapshot=snapshot
            )
            final_score = float(adjusted.get(picked, score_map.get(picked, 0.0)))
        else:
            from src.utils.ai import pick_best_strategy_with_priority

            picked, adjusted = pick_best_strategy_with_priority(score_map, snapshot)
            if picked == "none":
                best_score, best_opp = max(scored, key=lambda x: x[0])
                picked = str(best_opp["strategy"])
                final_score = best_score
            else:
                final_score = float(adjusted.get(picked, score_map.get(picked, 0.0)))

        logger.info(
            "StrategyBrain | best=%s score=%.1f gates=%s vol=%.3f%% scores=%s",
            picked,
            final_score,
            gates.get("mode"),
            float(gates.get("vol_5m") or 0.0),
            score_map,
        )

        result = {
            "strategy": picked,
            "score": final_score,
            "gates": gates,
            "scores": score_map,
            "adjusted_scores": adjusted,
            "source": "strategy_brain",
        }
        if _env_bool("ENABLE_ML_REGIME_DETECTOR", False):
            try:
                from src.ml.regime_detector import ensemble_weights_for_regime, predict_regime

                gross = float(
                    (snapshot or {}).get("cex_dex", {}).get("gross_bps")
                    or 0.0
                )
                net = float((snapshot or {}).get("cex_dex", {}).get("net_bps") or 0.0)
                regime, probs = predict_regime(gross, net)
                result["regime"] = regime
                result["regime_probs"] = probs
                result["ensemble_weights"] = ensemble_weights_for_regime(regime)
            except Exception as exc:
                logger.debug("regime detector skipped: %s", exc)
        return self._apply_go_live_fill_bias(result, snapshot)

    def _apply_go_live_fill_bias(
        self,
        result: dict[str, Any],
        snapshot: dict | None,
    ) -> dict[str, Any]:
        """Prefer fill-capable CEX/DEX lanes over inactive webhook/collateral picks."""
        if not _env_bool("GO_LIVE_SMALL_ACCOUNT", False):
            return result
        picked = str(result.get("strategy") or "")
        if picked in ("cex_dex", "dex_cex_reverse"):
            return result
        from src.strategies.brain_signals import (
            backrun_signal_present,
            collateral_signal_present,
            dex_cex_reverse_signal_present,
            lane_signal_present,
        )

        if picked == "backrun" and backrun_signal_present(snapshot):
            return result
        if picked == "collateral_swap" and collateral_signal_present(snapshot):
            return result
        if picked == "liquidation" and lane_signal_present(snapshot, "liquidation"):
            return result
        if dex_cex_reverse_signal_present(snapshot):
            result["strategy"] = "dex_cex_reverse"
        else:
            result["strategy"] = "cex_dex"
        result["go_live_fill_override"] = True
        return result

    async def select_from_snapshot(self, snapshot: dict) -> dict[str, Any]:
        """Build opportunities from snapshot and return the best lane."""
        opportunities = self.build_opportunities_from_snapshot(snapshot)
        return await self.select_best_strategy(opportunities, snapshot=snapshot)
