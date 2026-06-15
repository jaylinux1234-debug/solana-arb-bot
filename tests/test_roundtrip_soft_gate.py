"""Roundtrip soft pass band (85% of min net)."""

from src.strategies.cex_dex_roundtrip import roundtrip_net_gate_passes


def test_roundtrip_strict_pass() -> None:
    ok, near = roundtrip_net_gate_passes(1.0, 0.8)
    assert ok is True
    assert near is False


def test_roundtrip_soft_band_pass() -> None:
    ok, near = roundtrip_net_gate_passes(0.75, 0.8)
    assert ok is True
    assert near is True


def test_roundtrip_fail_below_soft() -> None:
    ok, near = roundtrip_net_gate_passes(0.5, 0.8)
    assert ok is False
    assert near is False
