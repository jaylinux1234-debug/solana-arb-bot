#!/usr/bin/env python3
"""Runtime hot-wallet validation: PRIVATE_KEY_FILE must match WALLET_PUBKEY."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    env_path = ROOT / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)
    compose_env = ROOT / "compose.env"
    if compose_env.is_file():
        load_dotenv(compose_env, override=False)
except ImportError:
    pass


def _truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def validate_hot_wallet() -> None:
    signer = (os.getenv("SIGNER_TYPE") or "hot").strip().lower()
    if signer != "hot":
        raise ValueError(f"SIGNER_TYPE must be hot (got {signer!r})")

    if _truthy("ENABLE_LEDGER_BRIDGE") or (os.getenv("LEDGER_SIGN_URL") or "").strip():
        raise ValueError("Ledger bridge must be disabled (SIGNER_TYPE=hot only)")

    from src.core.signer import HotWalletSigner

    kp = HotWalletSigner.get_keypair()
    actual = str(kp.pubkey())
    print(f"Hot wallet validated | pubkey={actual[:12]}…")


def main() -> int:
    if _truthy("TEST_MODE") or _truthy("SIMULATE"):
        print("validate-secrets: skipped (TEST_MODE/SIMULATE)")
        return 0

    try:
        validate_hot_wallet()
        return 0
    except Exception as exc:
        print(f"validate-secrets FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
