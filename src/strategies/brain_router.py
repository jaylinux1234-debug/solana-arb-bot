"""Direction-aware lane selection (DEX-cheap regime + score comparison)."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

NEAR_MISS_PATH = Path(os.getenv("CEX_DEX_NEAR_MISS_PATH", "logs/cex_dex_near_misses.jsonl"))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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


class BrainRouter:
    """Pick ``cex_dex`` vs ``dex_cex_reverse`` using scores and near-miss direction history."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.config = settings or get_settings()

    @property
    def priority_bias(self) -> float:
        raw = os.getenv("CEX_DEX_BRAIN_PRIORITY_BIAS", "").strip()
        if raw:
            return float(raw)
        return float(getattr(self.config, "CEX_DEX_BRAIN_PRIORITY_BIAS", 35.0))

    def recent_wrong_direction_count(
        self,
        direction: str = "dex_cheap",
        *,
        window_minutes: float | None = None,
        max_lines: int | None = None,
    ) -> int:
        """
        Count recent near-misses whose reason indicates the wrong lane for ``direction``.

        ``dex_cheap`` → ``wrong_direction_dex_cheap`` in ``cex_dex_near_misses.jsonl``.
        """
        if not NEAR_MISS_PATH.is_file():
            return 0

        window = window_minutes
        if window is None:
            window = _env_float("BRAIN_WRONG_DIRECTION_WINDOW_MIN", 30.0)
        max_scan = max_lines if max_lines is not None else _env_int("BRAIN_WRONG_DIRECTION_MAX_LINES", 800)
        cutoff = datetime.now(UTC) - timedelta(minutes=window)
        needle = "dex_cheap" if direction == "dex_cheap" else direction.lower()

        try:
            lines = NEAR_MISS_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return 0

        count = 0
        for line in reversed(lines[-max_scan:]):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("timestamp")
            if ts:
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    if dt < cutoff:
                        break
                except ValueError:
                    pass
            reason = str(row.get("reason") or "").lower()
            if direction == "dex_cheap":
                if "wrong_direction_dex_cheap" in reason:
                    count += 1
            elif needle in reason or f"wrong_direction_{needle}" in reason:
                count += 1
        return count

    async def pick_strategy_with_direction_awareness(
        self,
        signals: dict[str, float] | dict[str, Any],
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> str:
        """
        Enhanced picker respecting market direction (``cex_dex`` vs ``dex_cex_reverse``).

        Other lanes in ``signals`` are ignored; use :meth:`pick_primary_lane` for full map.
        """
        cex_dex_score = float(signals.get("cex_dex", 0) or 0)
        reverse_score = float(signals.get("dex_cex_reverse", 0) or 0)

        threshold = _env_int("BRAIN_DEX_CHEAP_REGIME_THRESHOLD", 8)
        boost = _env_float("BRAIN_DEX_CHEAP_REVERSE_BOOST", 45.0)
        bias_factor = _env_float("BRAIN_PRIORITY_BIAS_FACTOR", 0.6)

        dex_cheap_misses = self.recent_wrong_direction_count("dex_cheap")
        snapshot_boost = 0.0
        rev_ctx = (snapshot or {}).get("dex_cex_reverse") or {}
        if rev_ctx.get("is_dex_cheap") or str(rev_ctx.get("direction", "")).lower() == "dex_cheap":
            snapshot_boost = boost * 0.5

        from src.strategies.direction_aware_brain import DirectionAwareBrain

        dab = DirectionAwareBrain(settings=self.config)
        cex_ctx = (snapshot or {}).get("cex_dex") if isinstance((snapshot or {}).get("cex_dex"), dict) else {}
        forward_signal = {
            "gross_bps": cex_ctx.get("gross_bps") or cex_ctx.get("spread_bps_gross"),
            "net_bps": cex_ctx.get("net_bps") or cex_ctx.get("spread_bps_net"),
            "ai_confidence": cex_ctx.get("confidence") or cex_ctx.get("ai_conf"),
            "direction": cex_ctx.get("direction"),
        }
        regime = dab.detect_regime(forward_signal, snapshot=snapshot)
        if regime == "dex_cheap" and dab.evaluate(dict(forward_signal), snapshot=snapshot):
            cex_dex_score += dab.soft_forward_score_boost()

        if dex_cheap_misses > threshold:
            logger.info(
                "DEX-cheap regime detected → boosting reverse lane | near_misses=%s threshold=%s",
                dex_cheap_misses,
                threshold,
            )
            reverse_score += boost

        reverse_score += snapshot_boost

        bias_margin = self.priority_bias * bias_factor
        if reverse_score > cex_dex_score + bias_margin:
            selected = "dex_cex_reverse"
        elif cex_dex_score >= reverse_score:
            selected = "cex_dex"
        else:
            selected = "dex_cex_reverse"

        logger.info(
            "Selected lane: %s | cex_score=%.1f reverse=%.1f bias_margin=%.1f "
            "dex_cheap_near_misses=%s snapshot_boost=%.1f",
            selected,
            cex_dex_score,
            reverse_score,
            bias_margin,
            dex_cheap_misses,
            snapshot_boost,
        )
        return selected

    async def pick_primary_lane(
        self,
        score_map: dict[str, float],
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, float]]:
        """
        Direction-aware pick for CEX-DEX family; defer to highest other lane if it dominates.
        """
        from src.utils.ai import pick_best_strategy_with_priority

        picked, adjusted = pick_best_strategy_with_priority(score_map, snapshot)

        margin = _env_float("BRAIN_OTHER_LANE_DOMINANCE_MARGIN", 15.0)
        other_max = max(
            (float(adjusted.get(k, 0)) for k in adjusted if k not in ("cex_dex", "dex_cex_reverse")),
            default=0.0,
        )
        family = {
            "cex_dex": float(adjusted.get("cex_dex", score_map.get("cex_dex", 0))),
            "dex_cex_reverse": float(
                adjusted.get("dex_cex_reverse", score_map.get("dex_cex_reverse", 0))
            ),
        }
        family_max = max(family.values())

        if other_max > family_max + margin:
            logger.info(
                "Direction router: keeping %s (other_lane=%.1f > family_max=%.1f)",
                picked,
                other_max,
                family_max,
            )
            return picked, adjusted

        if picked not in ("cex_dex", "dex_cex_reverse", "none"):
            return picked, adjusted

        dir_pick = await self.pick_strategy_with_direction_awareness(adjusted, snapshot=snapshot)
        if picked != dir_pick:
            logger.info("Direction-aware override: %s → %s", picked, dir_pick)
        adjusted = dict(adjusted)
        adjusted["direction_override_from"] = picked
        return dir_pick, adjusted
