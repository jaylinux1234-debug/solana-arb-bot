# src/core/secure_secrets.py
"""Production signer policy: SOPS hot wallet only (Ledger removed)."""

from __future__ import annotations

import os

from src.core.security import is_placeholder_secret

__all__ = [
    "enforce_production_signer_policy",
    "is_production",
    "signer_type",
    "skip_hot_secret_files",
    "validate_signer_config",
]


def is_production() -> bool:
    return (os.getenv("APP_ENV") or "").strip().lower() in ("production", "prod")


def signer_type() -> str:
    return (os.getenv("SIGNER_TYPE") or "hot").strip().lower()


def skip_hot_secret_files() -> bool:
    """Always load hot keys from SOPS secret files when ``SIGNER_TYPE=hot``."""
    return False


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _truthy_env(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


def _strip_hot_keys_from_environ() -> None:
    for key in ("PRIVATE_KEY", "PRIVATE_KEY_CEX_DEX"):
        os.environ.pop(key, None)


def enforce_production_signer_policy() -> None:
    """
    Fail closed in production:

    - ``SIGNER_TYPE`` must be ``hot`` (Ledger support removed).
    - ``LEDGER_SIGN_URL`` / ``ENABLE_LEDGER_BRIDGE`` must be off.
    - No inline ``PRIVATE_KEY`` in environment — use ``PRIVATE_KEY_FILE`` (SOPS).
    """
    if not is_production():
        return

    if signer_type() != "hot":
        raise RuntimeError(
            "Production requires SIGNER_TYPE=hot (Ledger removed; use SOPS private_key file)."
        )

    if _truthy_env("ENABLE_LEDGER_BRIDGE") or _env("LEDGER_SIGN_URL"):
        raise RuntimeError(
            "Ledger bridge disabled — unset LEDGER_SIGN_URL and ENABLE_LEDGER_BRIDGE."
        )

    if _truthy_env("ALLOW_HOT_KEY_IN_PROD"):
        return

    for key in ("PRIVATE_KEY", "PRIVATE_KEY_CEX_DEX"):
        if _env(key) and not is_placeholder_secret(_env(key)):
            raise RuntimeError(
                f"Production forbids hot key in env ({key}); use SOPS secret files only."
            )


def validate_signer_config() -> None:
    """Call after dotenv + secret files are loaded."""
    enforce_production_signer_policy()
