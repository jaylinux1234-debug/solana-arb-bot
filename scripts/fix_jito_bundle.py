#!/usr/bin/env python3
"""
Jito Bundle Reliability Fixes — ensure tipping / multi-region env defaults are set.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def apply_jito_fixes() -> None:
    """Apply Jito reliability defaults (idempotent)."""
    os.environ.setdefault("JITO_DYNAMIC_TIP", "true")
    os.environ.setdefault("JITO_TIP_FILL_RATE_TARGET", "0.65")
    os.environ.setdefault("JITO_TIP_FILL_RATE_MAX_BOOST", "0.35")
    os.environ.setdefault("JITO_TIP_PROFIT_RATIO", "0.58")
    os.environ.setdefault("JITO_TIP_MIN_LAMPORTS", "60000")
    os.environ.setdefault("JITO_TIP_MAX_LAMPORTS", "280000")
    os.environ.setdefault("JITO_SUBMIT_MULTI_REGION", "true")
    os.environ.setdefault("JITO_APPEND_TIP_TX", "true")
    os.environ.setdefault("JITO_RPC_FALLBACK_ON_FAIL", "true")
    os.environ.setdefault("V2_STRONG_JITO_TIP_MULT", "1.15")

    # Touch executor so import path stays valid for ops smoke checks.
    from src.execution.jito import JitoBundleExecutor  # noqa: F401

    print("Jito fixes applied:")
    print("   - Dynamic tip with higher fill target")
    print("   - Multi-region submission")
    print("   - Tip tx appended + fallback RPC")


if __name__ == "__main__":
    asyncio.run(apply_jito_fixes())
