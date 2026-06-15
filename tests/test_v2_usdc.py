"""USDC manager sizing tests."""

from __future__ import annotations

from src.v2.config import V2Config
from src.v2.usdc_manager import USDCManager


def test_trade_size_micro_caps_and_buffers():
    cfg = V2Config(min_trade_usdc=6.0, max_trade_usdc_micro=12_000_000)
    mgr = USDCManager(cfg)
    micro = mgr.trade_size_micro(20.0, 12_000_000)
    assert micro == 12_000_000  # capped at signal max 12


def test_trade_size_respects_minimum():
    cfg = V2Config(min_trade_usdc=6.0)
    mgr = USDCManager(cfg)
    micro = mgr.trade_size_micro(9.0, 12_000_000)
    assert micro >= 6_000_000
