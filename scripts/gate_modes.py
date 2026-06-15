#!/usr/bin/env python3
"""Switch CEX-DEX gate tiers in ``.env`` (Plan 2: controlled testing)."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MODES: dict[str, dict[str, str]] = {
    "strict": {
        "CEX_DEX_MIN_GROSS_SPREAD_BPS": "12",
        "CEX_DEX_MIN_NET_SPREAD_BPS": "5",
        "CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS": "5",
        "CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC": "0.40",
        "AI_APPROVE_MIN_CONFIDENCE": "75",
        "CEX_DEX_MAX_TRADE_USDC_MICRO": "12000000",
        "LIVE_TRADE_COOLDOWN_SECONDS": "50",
    },
    "opportunistic": {
        "CEX_DEX_MIN_GROSS_SPREAD_BPS": "8",
        "CEX_DEX_MIN_NET_SPREAD_BPS": "2",
        "CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS": "2",
        "CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC": "0.35",
        "AI_APPROVE_MIN_CONFIDENCE": "68",
        "CEX_DEX_MAX_TRADE_USDC_MICRO": "12000000",
        "LIVE_TRADE_COOLDOWN_SECONDS": "90",
    },
    "first-fill": {
        "CEX_DEX_MIN_GROSS_SPREAD_BPS": "6",
        "CEX_DEX_MIN_NET_SPREAD_BPS": "0",
        "CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS": "0",
        "CEX_DEX_ROUNDTRIP_SIM_MIN_RETAIN_FRAC": "0.25",
        "AI_APPROVE_MIN_CONFIDENCE": "60",
        "CEX_DEX_MAX_TRADE_USDC_MICRO": "6000000",
        "LIVE_TRADE_COOLDOWN_SECONDS": "300",
    },
}


def _apply_updates(content: str, updates: dict[str, str]) -> str:
    """Replace active ``KEY=value`` lines; append any keys missing from ``.env``."""
    pending = dict(updates)
    out: list[str] = []
    key_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")

    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            out.append(line)
            continue
        match = key_re.match(stripped)
        if match and match.group(1) in pending:
            key = match.group(1)
            newline = "\n" if line.endswith("\n") else ""
            out.append(f"{key}={pending.pop(key)}{newline}")
        else:
            out.append(line)

    if pending:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"\n# === gate_modes.py ({updates}) ===\n")
        for key, value in pending.items():
            out.append(f"{key}={value}\n")

    return "".join(out)


def set_mode(mode: str, *, env_path: Path | None = None) -> None:
    if mode not in MODES:
        raise SystemExit(
            f"Unknown mode {mode!r}. Choose: {', '.join(sorted(MODES))}"
        )

    path = env_path or (ROOT / ".env")
    if not path.is_file():
        raise SystemExit(f"Missing {path} (copy from .env.example)")

    updated = _apply_updates(path.read_text(encoding="utf-8"), MODES[mode])
    path.write_text(updated, encoding="utf-8")

    print(f"Switched .env to {mode!r} mode:")
    for key, value in MODES[mode].items():
        print(f"  {key}={value}")
    print()
    print("Next:")
    print("  npm run sync:compose-env")
    print("  npm run compose:prod:restart:no-build")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply CEX-DEX gate tier to .env (strict | opportunistic | first-fill)."
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="strict",
        choices=sorted(MODES),
        help="Gate profile (default: strict)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to .env (default: repo root .env)",
    )
    args = parser.parse_args(argv)
    set_mode(args.mode, env_path=args.env_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
