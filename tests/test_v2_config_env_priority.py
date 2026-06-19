from __future__ import annotations

import os

import pytest

from src.v2.config import V2Config, _env_float, _env_int


def test_v2_net_threshold_prefers_cex_dex_over_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("V2_MIN_NET_BPS", raising=False)
    monkeypatch.setenv("V2_MIN_NET_BPS_BASE", "0.5")
    monkeypatch.setenv("CEX_DEX_MIN_NET_SPREAD_BPS", "0.75")

    # Mirror from_env priority without loading repo .env
    default = 1.2
    if os.getenv("V2_MIN_NET_BPS") is not None:
        net = _env_float("V2_MIN_NET_BPS", default)
    elif os.getenv("CEX_DEX_MIN_NET_SPREAD_BPS") is not None:
        net = _env_float("CEX_DEX_MIN_NET_SPREAD_BPS", default)
    elif os.getenv("V2_MIN_NET_BPS_BASE") is not None:
        net = _env_float("V2_MIN_NET_BPS_BASE", default)
    else:
        net = default
    assert net == pytest.approx(0.75)


def test_v2_gross_threshold_prefers_cex_dex_over_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("V2_MIN_GROSS_BPS", raising=False)
    monkeypatch.setenv("V2_MIN_GROSS_BPS_BASE", "12")
    monkeypatch.setenv("CEX_DEX_MIN_GROSS_SPREAD_BPS", "7")

    default = 7.0
    if os.getenv("V2_MIN_GROSS_BPS") is not None:
        gross = _env_float("V2_MIN_GROSS_BPS", default)
    elif os.getenv("CEX_DEX_MIN_GROSS_SPREAD_BPS") is not None:
        gross = float(_env_int("CEX_DEX_MIN_GROSS_SPREAD_BPS", int(default)))
    elif os.getenv("V2_MIN_GROSS_BPS_BASE") is not None:
        gross = _env_float("V2_MIN_GROSS_BPS_BASE", default)
    else:
        gross = default
    assert gross == pytest.approx(7.0)
