"""execute_reverse_arb P&L helpers."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from src.v2.config import V2Config
from src.v2.dex_cex_reverse import V2ReverseLane


class _StubReverse:
    backpack = None


def test_calculate_net_pnl_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pnl_log = tmp_path / "v2_pnl.jsonl"
    monkeypatch.setenv("V2_PNL_LOG", str(pnl_log))

    lane = V2ReverseLane(_StubReverse(), V2Config())  # type: ignore[arg-type]
    net = lane.calculate_net_pnl(
        Decimal("12"),
        Decimal("12.05"),
        Decimal("0.01"),
        {"jupiter": Decimal("0.005"), "cex": Decimal("0.005")},
        signal={"gross_bps": 12.0, "net_bps": 1.5, "trade_usdc": 12.0},
        tx_sig="test_tx_sig",
    )
    assert net == pytest.approx(Decimal("0.03"))
    lines = pnl_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["net_usdc"] == pytest.approx(0.03)
    assert row["tx_sig"] == "test_tx_sig"
    assert row["gross_bps"] == 12.0


def test_resolve_execution_send_path():
    lane = V2ReverseLane(_StubReverse(), V2Config())  # type: ignore[arg-type]
    assert lane._resolve_execution_send_path(12.0) == "jito"
    assert lane._resolve_execution_send_path(8.0) == "rpc"
    assert lane._resolve_execution_send_path(5.0, strong_signal=True) == "jito"


def test_required_cex_sol_buffer():
    lane = V2ReverseLane(_StubReverse(), V2Config())  # type: ignore[arg-type]
    required = lane._required_cex_sol_from_signal(
        {"trade_usdc": 12.0, "cex_bid": 60.0, "size_usdc_micro": 12_000_000}
    )
    # 12/60 * 1.02 = 0.204
    assert Decimal("0.20") < required < Decimal("0.21")
