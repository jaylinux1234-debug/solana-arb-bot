"""Safe reads from Settings (UPPER_CASE env fields)."""

from __future__ import annotations

from typing import Any


def settings_int(cfg: Any, *names: str, default: int = 0) -> int:
    for name in names:
        raw = getattr(cfg, name, None)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    return int(default)


def settings_float(cfg: Any, *names: str, default: float = 0.0) -> float:
    for name in names:
        raw = getattr(cfg, name, None)
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    return float(default)
