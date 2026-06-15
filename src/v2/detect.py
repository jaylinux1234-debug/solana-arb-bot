"""Detect dex-cheap SOL reverse opportunity (v2.1 lane)."""

from __future__ import annotations

from typing import Any

from src.strategies.dex_cex_reverse import DexCexReverseStrategy
from src.v2.config import V2Config
from src.v2.dex_cex_reverse import V2ReverseLane


async def detect_dex_cheap(
    reverse: DexCexReverseStrategy,
    cfg: V2Config,
) -> dict[str, Any] | None:
    """Return opportunity dict or None (delegates to ``V2ReverseLane``)."""
    lane = V2ReverseLane(reverse, cfg)
    return await lane.detect_dex_cheap_signal()
