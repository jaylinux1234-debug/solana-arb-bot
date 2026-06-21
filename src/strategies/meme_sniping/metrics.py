"""In-process sim metrics for meme sniping (hourly reports via logs)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPORT_PATH = Path("logs/meme_sniping_sim.jsonl")


@dataclass
class MemeSnipingMetrics:
    started_at: float = field(default_factory=time.time)
    scans: int = 0
    candidates_seen: int = 0
    dex_fallback_scans: int = 0
    _last_summary_at: float = field(default_factory=time.time)
    filter_rejects: dict[str, int] = field(default_factory=dict)
    ai_reviews: int = 0
    ai_approvals: int = 0
    sim_entries: int = 0
    sim_exits: dict[str, int] = field(default_factory=dict)
    active_positions: int = 0
    last_source: str = ""
    last_signal: dict[str, Any] | None = None

    def record_scan(self, source: str, candidate_count: int) -> None:
        self.scans += 1
        self.last_source = source
        self.candidates_seen += candidate_count
        if source == "dexscreener":
            self.dex_fallback_scans += 1

    def record_reject(self, reason: str) -> None:
        self.filter_rejects[reason] = self.filter_rejects.get(reason, 0) + 1

    def record_ai(self, approved: bool) -> None:
        self.ai_reviews += 1
        if approved:
            self.ai_approvals += 1

    def record_entry(self, mint: str, size_sol: float, confidence: float) -> None:
        self.sim_entries += 1
        self.active_positions += 1
        self.last_signal = {
            "mint": mint[:12],
            "size_sol": round(size_sol, 3),
            "confidence": round(confidence, 1),
            "ts": datetime.now(UTC).isoformat(),
        }

    def record_exit(self, reason: str) -> None:
        key = reason.split("(")[0].strip() or reason
        self.sim_exits[key] = self.sim_exits.get(key, 0) + 1
        self.active_positions = max(0, self.active_positions - 1)

    def snapshot(self) -> dict[str, Any]:
        elapsed_min = round((time.time() - self.started_at) / 60.0, 1)
        return {
            "elapsed_min": elapsed_min,
            "scans": self.scans,
            "candidates_seen": self.candidates_seen,
            "last_source": self.last_source,
            "dex_fallback_scans": self.dex_fallback_scans,
            "filter_rejects": dict(self.filter_rejects),
            "ai_reviews": self.ai_reviews,
            "ai_approvals": self.ai_approvals,
            "approval_rate_pct": round(
                (self.ai_approvals / self.ai_reviews * 100.0) if self.ai_reviews else 0.0, 1
            ),
            "sim_entries": self.sim_entries,
            "sim_exits": dict(self.sim_exits),
            "active_positions": self.active_positions,
            "last_signal": self.last_signal,
        }

    def log_summary_if_due(self, interval_sec: float = 300.0) -> None:
        """Emit periodic summary every ``interval_sec`` (default 5 min)."""
        now = time.time()
        if self.scans == 0 or now - self._last_summary_at < interval_sec:
            return
        self._last_summary_at = now
        snap = self.snapshot()
        logger.info("meme_sniping_sim_summary | %s", json.dumps(snap, default=str))
        try:
            _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _REPORT_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": datetime.now(UTC).isoformat(), **snap}) + "\n")
        except OSError as exc:
            logger.debug("meme_sniping metrics write failed: %s", exc)


meme_sniping_metrics = MemeSnipingMetrics()
