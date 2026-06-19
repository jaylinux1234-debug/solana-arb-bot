from __future__ import annotations

import os

import pytest

from src.monitoring.cex_health import (
    get_cached_backpack_usdc,
    record_backpack_balances,
)
from src.monitoring.health import _health_config_snapshot
from src.v2.config import V2Config


def test_record_and_read_backpack_balance_cache() -> None:
    record_backpack_balances(41.39, 0.765)
    assert get_cached_backpack_usdc() == pytest.approx(41.39)


def test_v2_config_uses_cex_dex_min_net_when_v2_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("V2_MIN_NET_BPS", raising=False)
    monkeypatch.delenv("V2_MIN_NET_BPS_BASE", raising=False)
    monkeypatch.setenv("CEX_DEX_MIN_NET_SPREAD_BPS", "0.75")
    cfg = V2Config(
        min_net_bps_base=0.75,
        min_net_bps=0.75,
    )
    assert cfg.min_net_bps_base == pytest.approx(0.75)


def test_health_config_prefers_v2_max_flash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("V2_MAX_FLASH_USDC", "35")
    monkeypatch.setenv("MAX_FLASH_USDC", "12")
    monkeypatch.setenv("CEX_DEX_MIN_NET_SPREAD_BPS", "0.75")
    monkeypatch.delenv("V2_MIN_NET_BPS", raising=False)
    monkeypatch.delenv("V2_MIN_NET_BPS_BASE", raising=False)

    from src.config.settings import Settings

    cfg = Settings()
    snap = _health_config_snapshot(cfg)
    assert snap["max_flash_usdc"] == pytest.approx(35.0)
    assert snap["v2_max_flash_usdc"] == pytest.approx(35.0)
    assert snap["min_net_bps"] == pytest.approx(0.75)
