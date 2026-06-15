"""auto_tuner gate regime detection."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.auto_tuner import auto_tune_gates


def _write_attempts(path: Path, gross_values: list[float]) -> None:
    lines = []
    for i, gross in enumerate(gross_values):
        lines.append(
            json.dumps(
                {
                    "cycle": i,
                    "gross_bps": gross,
                    "net_bps": gross - 20,
                    "spread_direction": "dex_cheap",
                    "block_reason": "net_below_threshold",
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_hot_market_suggests_loosen(tmp_path: Path, monkeypatch):
    log = tmp_path / "attempts.jsonl"
    _write_attempts(log, [16.0, 18.0, 17.0, 15.5])
    monkeypatch.setenv("V2_MIN_NET_BPS_BASE", "0.4")
    monkeypatch.setenv("V2_MIN_NET_BPS", "0.5")
    report = auto_tune_gates(log, window=50)
    assert report["regime"] == "hot"
    assert report["avg_gross_bps"] > 15
    keys = {s["key"] for s in report["suggestions"]}
    assert "V2_MIN_NET_BPS" in keys or "V2_MIN_NET_BPS_BASE" in keys


def test_tight_market_keeps_strict(tmp_path: Path, monkeypatch):
    log = tmp_path / "attempts.jsonl"
    _write_attempts(log, [1.0, 2.0, 0.5, 1.5, 2.5])
    monkeypatch.setenv("V2_MIN_NET_BPS_BASE", "0.35")
    report = auto_tune_gates(log, window=50)
    assert report["regime"] == "tight"
    loosen = [s for s in report["suggestions"] if s["key"] == "V2_MIN_NET_BPS"]
    assert not loosen
