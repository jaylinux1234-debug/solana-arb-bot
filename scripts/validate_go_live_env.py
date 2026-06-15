#!/usr/bin/env python3
"""Preflight prod signer policy using .env (ignores stale shell SIGNER_TYPE=hotkey)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        print("ERROR: missing .env (copy from .env.example)", file=sys.stderr)
        return 1

    load_dotenv(env_path, override=True)

    for legacy in (".env.txt", ".env.txt.bak", ".ENV.txt"):
        if (ROOT / legacy).is_file():
            print(
                f"ERROR: {legacy} still present — run: npm run secrets:migrate",
                file=sys.stderr,
            )
            return 1

    for key in ("PRIVATE_KEY", "PRIVATE_KEY_CEX_DEX"):
        os.environ.pop(key, None)

    from src.core.secure_secrets import validate_signer_config

    validate_signer_config()

    def expect(key: str, *allowed: str) -> None:
        val = (os.getenv(key) or "").strip().lower()
        ok = {a.lower() for a in allowed}
        if val not in ok:
            raise SystemExit(f"ERROR: {key}={os.getenv(key)!r} expected one of {sorted(ok)}")

    expect("SIMULATE", "false", "0", "no")
    expect("SIGNER_TYPE", "hot")
    expect("ALLOW_HOT_KEY_IN_PROD", "0", "false", "", "no", "off")
    expect("KILL_SWITCH_ON_LOSS", "1", "true", "yes", "on")

    live = (os.getenv("LIVE_TRADING_CONFIRM") or "").strip().upper()
    if live != "YES":
        raise SystemExit("ERROR: LIVE_TRADING_CONFIRM must be YES for production launch")

    wallet = (os.getenv("WALLET_PUBKEY") or "").strip()
    if not wallet:
        raise SystemExit("ERROR: WALLET_PUBKEY must be set for live CEX-DEX")

    if (os.getenv("LEDGER_SIGN_URL") or "").strip():
        raise SystemExit("ERROR: LEDGER_SIGN_URL must be empty (Ledger support removed)")

    if (os.getenv("ENABLE_LEDGER_BRIDGE") or "").strip().lower() in ("1", "true", "yes", "on"):
        raise SystemExit("ERROR: ENABLE_LEDGER_BRIDGE must be false")

    def _truthy(name: str) -> bool:
        return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")

    small_account = _truthy("GO_LIVE_SMALL_ACCOUNT") or float(
        (os.getenv("MAX_FLASH_USDC") or "0").strip() or 0
    ) < 1000

    max_flash = float((os.getenv("MAX_FLASH_USDC") or "0").strip() or 0)
    max_trade_micro = int((os.getenv("CEX_DEX_MAX_TRADE_USDC_MICRO") or "0").strip() or 0)
    max_trade_usdc = max_trade_micro / 1_000_000.0 if max_trade_micro else max_flash

    if small_account:
        if max_trade_usdc < 5.0 and max_flash < 5.0:
            raise SystemExit(
                f"ERROR: small-account cap too low "
                f"(CEX_DEX_MAX_TRADE_USDC_MICRO={max_trade_micro}, MAX_FLASH_USDC={max_flash}); need >= 5 USDC"
            )
    elif max_flash < 10_000:
        raise SystemExit(
            f"ERROR: MAX_FLASH_USDC={max_flash!r} too low for institutional prod (need >= 10000) "
            f"or set GO_LIVE_SMALL_ACCOUNT=true"
        )

    min_net = int((os.getenv("CEX_DEX_MIN_NET_SPREAD_BPS") or "0").strip() or 0)
    min_net_floor = 3 if small_account else 40
    if min_net < min_net_floor:
        raise SystemExit(
            f"ERROR: CEX_DEX_MIN_NET_SPREAD_BPS={min_net} too low "
            f"(need >= {min_net_floor} for {'small' if small_account else 'institutional'} prod)"
        )

    ai_conf = float((os.getenv("AI_APPROVE_MIN_CONFIDENCE") or "0").strip() or 0)
    ai_floor = 68.0 if small_account else 80.0
    if ai_conf < ai_floor:
        raise SystemExit(
            f"ERROR: AI_APPROVE_MIN_CONFIDENCE={ai_conf} too low "
            f"(need >= {ai_floor:g} for {'small' if small_account else 'institutional'} prod)"
        )

    mode = "small-account" if small_account else "institutional"
    print(f"  signer + safety policy OK (.env) | mode={mode}")
    print(
        f"  ok LIVE_TRADING_CONFIRM=YES max_trade_usdc={max_trade_usdc:g} "
        f"min_net_bps={min_net} ai_conf={ai_conf}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
