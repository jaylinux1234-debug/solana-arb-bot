"""CEX-DEX inventory cap helpers (shared with ``cex_dex.py`` flash path)."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

CEX_DEX_MAX_INVENTORY_SOL = float(
    os.getenv(
        "CEX_DEX_MAX_INVENTORY_SOL",
        os.getenv("INVENTORY_MAX_SOL", "45"),
    )
)


def inventory_cap_blocks(
    current_inventory_sol: float,
    max_inventory_sol: float | None = None,
) -> bool:
    """Return True when logical CEX SOL inventory exceeds the configured cap."""
    cap = CEX_DEX_MAX_INVENTORY_SOL if max_inventory_sol is None else float(max_inventory_sol)
    if current_inventory_sol > cap:
        logger.warning(
            "Inventory cap hit | inventory_sol=%.4f max=%.4f",
            current_inventory_sol,
            cap,
        )
        return True
    return False
