#!/usr/bin/env python3
"""Docker, secrets, and deploy-address checks before prod compose up."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SECRET_NAMES = (
    "private_key",
    "private_key.txt",
    "private_key_cex_dex",
    "jupiter_api_key",
    "openai_api_key",
    "backpack_secret",
    "helius_api_key",
    "oneinch_api_key.txt",
    "cow_api_key.txt",
    "pagerduty_routing_key.txt",
)


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        raise SystemExit("ERROR: missing .env (copy from .env.example)")
    for legacy in (".env.txt", ".env.txt.bak", ".ENV.txt"):
        if (ROOT / legacy).is_file():
            raise SystemExit(f"ERROR: {legacy} still present — run: npm run secrets:migrate")
    load_dotenv(env_path, override=True)


def _docker_ok() -> None:
    if not shutil.which("docker"):
        raise SystemExit("ERROR: docker CLI not found — install Docker Desktop")
    r = subprocess.run(
        ["docker", "info"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        raise SystemExit(f"ERROR: docker daemon not running — {msg[:200]}")


def _check_live_confirm() -> None:
    confirm = (os.getenv("LIVE_TRADING_CONFIRM") or "").strip().upper()
    if confirm != "YES":
        raise SystemExit("ERROR: LIVE_TRADING_CONFIRM must be YES in .env for production launch")


def _check_deploy_addresses() -> None:
    deployer = (
        os.getenv("DEPLOYER_PRIVATE_KEY") or os.getenv("BASE_DEPLOYER_PRIVATE_KEY") or ""
    ).strip()
    owner = (os.getenv("GNOSIS_SAFE_ADDRESS") or os.getenv("TIMELOCK_ADDRESS") or "").strip()
    if not deployer:
        print("  WARN: DEPLOYER_PRIVATE_KEY unset (required only for npm run deploy:secure)")
    else:
        print("  ok DEPLOYER_PRIVATE_KEY set (Base deploy)")
    if not owner:
        print("  WARN: GNOSIS_SAFE_ADDRESS / TIMELOCK_ADDRESS unset (deploy:secure)")
    else:
        print(f"  ok contract owner candidate={owner[:10]}...")


def _secret_populated(name: str) -> bool:
    for base in (ROOT / "secrets", ROOT / "secrets" / ".local"):
        path = base / name
        if path.is_file() and path.stat().st_size > 0:
            return True
    return False


def _check_secrets() -> int:
    missing = 0
    for name in SECRET_NAMES:
        if _secret_populated(name):
            print(f"  ok secrets/{name}")
        else:
            print(f"  WARN: secrets/{name} empty (populate secrets/.local/ then npm run secrets:sync-local)")
            missing += 1
    if os.getenv("REQUIRE_POPULATED_SECRETS", "").strip() in ("1", "true", "yes"):
        if missing:
            raise SystemExit(f"ERROR: {missing} secret(s) empty with REQUIRE_POPULATED_SECRETS=1")
    return missing


def main() -> int:
    _load_env()
    print("=== Env policy (validate_go_live_env) ===")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_go_live_env.py")],
        cwd=ROOT,
    )
    if r.returncode != 0:
        raise SystemExit(r.returncode)
    print("=== Docker ===")
    _docker_ok()
    print("  ok docker daemon")
    print("\n=== Live trading confirm ===")
    _check_live_confirm()
    print("  ok LIVE_TRADING_CONFIRM=YES")
    print("\n=== Deploy addresses ===")
    _check_deploy_addresses()
    print("\n=== secrets/ ===")
    _check_secrets()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
