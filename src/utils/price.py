"""Price / spread helpers for CEX-DEX."""

from __future__ import annotations


def bps_diff(a: float, b: float) -> float:
    """Signed spread in bps: ``(b - a) / a * 10_000`` (positive if ``b > a``)."""
    if a == 0:
        return 0.0
    return (b - a) / a * 10000.0
