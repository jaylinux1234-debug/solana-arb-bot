#!/usr/bin/env python3
"""Log on-chain USDC snapshot to logs/capital_delta.jsonl (Phase 1 tracking)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        fn = getattr(stream, "reconfigure", None)
        if callable(fn):
            try:
                fn.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


async def main() -> int:
    _stdio_utf8()
    parser = argparse.ArgumentParser(description="Track on-chain USDC capital delta")
    parser.add_argument(
        "--action",
        default="manual_check",
        help="Label for this snapshot (default: manual_check)",
    )
    args = parser.parse_args()

    from src.config.settings import bootstrap_config
    from src.monitoring.capital_delta import log_capital_delta

    bootstrap_config()
    row = await log_capital_delta(args.action)
    if not row:
        print("Capital delta logging disabled or balance read failed")
        return 1
    print(
        f"onchain_usdc=${row.get('onchain_usdc')} "
        f"delta={row.get('delta_usdc')} action={row.get('action')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
