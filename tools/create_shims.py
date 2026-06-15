#!/usr/bin/env python3
"""Deprecated: root shims were removed. Import from ``src.*`` directly."""

from __future__ import annotations

import sys

SHIM_TO_SRC = {
    "cex_executor": "src.cex.executor",
    "jupiter_executor": "src.dex.jupiter",
    "openai_helper": "src.utils.ai",
    "cex_dex_strategy": "src.strategies.cex_dex",
    "wallet_safety": "src.core.wallet_safety",
    "health_api": "src.utils.health",
}

if __name__ == "__main__":
    print("Root shims are removed. Use src imports, e.g.:", file=sys.stderr)
    for old, new in sorted(SHIM_TO_SRC.items()):
        print(f"  {old} -> {new}", file=sys.stderr)
    raise SystemExit(1)
