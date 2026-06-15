#!/usr/bin/env python3
"""
Auto-switch CEX-DEX gate tier (strict ↔ opportunistic) from 5m CEX volatility.

Uses ``scripts/gate_modes.py`` profiles and ``src/strategies/volatility_gate`` samples.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE_PATH = Path(os.getenv("AUTO_GATE_STATE_PATH", "logs/auto_gate_mode.txt"))
POLL_SEC = float(os.getenv("AUTO_GATE_POLL_SEC", "300"))
VOL_THRESHOLD_PCT = float(os.getenv("CEX_DEX_OPPORTUNISTIC_VOL_PCT", "0.8"))


def _read_state() -> str | None:
    if not STATE_PATH.exists():
        return None
    raw = STATE_PATH.read_text(encoding="utf-8").strip().lower()
    return raw if raw in ("strict", "opportunistic") else None


def _write_state(mode: str) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(mode + "\n", encoding="utf-8")


def _gate_modes():
    import importlib.util

    path = ROOT / "scripts" / "gate_modes.py"
    spec = importlib.util.spec_from_file_location("gate_modes", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def switch_to_opportunistic(*, env_path: Path | None = None) -> bool:
    """Apply opportunistic gate profile to ``.env``."""
    set_mode = _gate_modes().set_mode

    if _read_state() == "opportunistic":
        print("Already in OPPORTUNISTIC mode (no .env change)")
        return False
    set_mode("opportunistic", env_path=env_path)
    _write_state("opportunistic")
    print("Switched to OPPORTUNISTIC mode")
    return True


def switch_to_strict(*, env_path: Path | None = None) -> bool:
    """Revert to strict gate profile."""
    set_mode = _gate_modes().set_mode

    if _read_state() == "strict":
        print("Already in STRICT mode (no .env change)")
        return False
    set_mode("strict", env_path=env_path)
    _write_state("strict")
    print("Reverted to STRICT mode")
    return True


async def fetch_volatility_pct() -> float | None:
    """5m CEX volatility % via ``VolatilityGate``."""
    from src.config.settings import get_settings
    from src.cex.backpack import BackpackClient
    from src.strategies.volatility_gate import get_volatility_gate

    get_settings()
    bp = BackpackClient()
    try:
        gate = get_volatility_gate(bp)
        return await gate.get_5min_volatility()
    finally:
        await bp.close()


def decide_mode(vol_pct: float | None) -> str | None:
    """
    Return gate_modes profile name from volatility tier.

    Maps VolatilityGate tiers to ``gate_modes.py`` profiles.
    """
    if vol_pct is None:
        return None
    aggressive = float(os.getenv("CEX_VOL_AGGRESSIVE_PCT", "1.2"))
    if vol_pct > aggressive:
        return "first-fill"
    if vol_pct > VOL_THRESHOLD_PCT:
        return "opportunistic"
    return "strict"


async def run_once(*, env_path: Path | None = None, sync_compose: bool = False) -> str | None:
    """Evaluate vol once and switch gates if needed. Returns mode applied or None."""
    vol = await fetch_volatility_pct()
    target = decide_mode(vol)
    print(
        f"vol_5m={vol if vol is not None else 'n/a'}% "
        f"threshold={VOL_THRESHOLD_PCT}% -> target={target or 'unchanged'}"
    )
    if target is None:
        return None

    changed = False
    if target == "opportunistic":
        changed = switch_to_opportunistic(env_path=env_path)
    else:
        changed = switch_to_strict(env_path=env_path)

    if changed and sync_compose:
        import subprocess

        subprocess.run(
            ["npm", "run", "sync:compose-env"],
            cwd=ROOT,
            check=False,
        )
        print("Ran npm run sync:compose-env — restart monitor to apply:")
        print("  npm run compose:prod:restart:no-build")

    return target


async def run_loop(
    *,
    env_path: Path | None = None,
    poll_sec: float = POLL_SEC,
    sync_compose: bool = False,
) -> None:
    print(
        f"Auto gate switcher running | poll={poll_sec}s "
        f"vol_threshold={VOL_THRESHOLD_PCT}%"
    )
    print("Ctrl+C to stop")
    while True:
        try:
            await run_once(env_path=env_path, sync_compose=sync_compose)
        except Exception as exc:
            print(f"Auto gate switcher error: {exc}")
        await asyncio.sleep(poll_sec)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Auto-switch .env gates strict/opportunistic from 5m CEX volatility."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one volatility check and exit",
    )
    parser.add_argument(
        "--force",
        choices=("strict", "opportunistic"),
        help="Force a mode (skip volatility check)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to .env (default: repo root .env)",
    )
    parser.add_argument(
        "--poll-sec",
        type=float,
        default=POLL_SEC,
        help=f"Loop interval seconds (default {POLL_SEC})",
    )
    parser.add_argument(
        "--sync-compose-env",
        action="store_true",
        help="Run npm run sync:compose-env after a mode change",
    )
    args = parser.parse_args(argv)

    if args.force:
        if args.force == "opportunistic":
            switch_to_opportunistic(env_path=args.env_file)
        else:
            switch_to_strict(env_path=args.env_file)
        if args.sync_compose_env:
            import subprocess

            subprocess.run(["npm", "run", "sync:compose-env"], cwd=ROOT, check=False)
        return 0

    if args.once:
        asyncio.run(
            run_once(env_path=args.env_file, sync_compose=args.sync_compose_env)
        )
        return 0

    try:
        asyncio.run(
            run_loop(
                env_path=args.env_file,
                poll_sec=args.poll_sec,
                sync_compose=args.sync_compose_env,
            )
        )
    except KeyboardInterrupt:
        print("\nAuto gate switcher stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
