# src/core/signer.py
"""
Hot wallet signer only (Ledger removed).

Loads ``PRIVATE_KEY_FILE`` / env via SOPS hydration with strict ``WALLET_PUBKEY`` check.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

from src.config.settings import get_settings

logger = logging.getLogger(__name__)


class HotWalletSigner:
    """Singleton hot-wallet keypair with security checks."""

    _instance: Keypair | None = None

    @classmethod
    def _enforce_hot_signer_policy(cls) -> None:
        signer_type = (os.getenv("SIGNER_TYPE") or "hot").strip().lower()
        if signer_type != "hot":
            raise RuntimeError(
                "Ledger support removed. Set SIGNER_TYPE=hot and use SOPS private_key."
            )
        if (os.getenv("LEDGER_SIGN_URL") or "").strip():
            raise RuntimeError("LEDGER_SIGN_URL must be empty (Ledger removed).")

    @classmethod
    def _load_key_material(cls) -> str:
        from src.core.security import load_secrets_from_files

        load_secrets_from_files()

        for env_name in ("PRIVATE_KEY_CEX_DEX", "PRIVATE_KEY"):
            material = (os.getenv(env_name) or "").strip()
            if material:
                return material

        key_path = (os.getenv("PRIVATE_KEY_FILE") or "").strip()
        if not key_path:
            cfg = get_settings()
            key_path = str(getattr(cfg, "PRIVATE_KEY_FILE", "") or "").strip()

        if not key_path:
            raise RuntimeError(
                "PRIVATE_KEY_FILE missing. Ensure SOPS secrets are mounted correctly."
            )

        path = Path(key_path)
        if not path.is_file():
            raise RuntimeError(f"PRIVATE_KEY_FILE not found: {key_path}")

        return path.read_text(encoding="utf-8").strip()

    @classmethod
    def get_keypair(cls) -> Keypair:
        if cls._instance is not None:
            return cls._instance

        cls._enforce_hot_signer_policy()

        from src.core.security import secure_load_keypair

        try:
            kp = secure_load_keypair(cls._load_key_material())
        except Exception as exc:
            logger.error("Failed to load private key: %s", exc)
            raise RuntimeError("Invalid private key format") from exc

        settings = get_settings()
        expected = (
            (os.getenv("WALLET_PUBKEY") or "").strip()
            or str(getattr(settings, "WALLET_PUBKEY", "") or "").strip()
            or str(getattr(settings, "wallet_pubkey", "") or "").strip()
        )
        actual = str(kp.pubkey())
        if expected and actual != expected:
            logger.critical(
                "WALLET_PUBKEY mismatch — expected %s got %s",
                expected,
                actual,
            )
            raise RuntimeError("Wallet pubkey mismatch — aborting for security")

        cls._instance = kp
        logger.info("Hot wallet signer loaded | pubkey=%s", actual)
        return kp

    @classmethod
    def reset(cls) -> None:
        """Clear cached keypair (tests / secret rotation)."""
        cls._instance = None

    @classmethod
    def sign_versioned(cls, tx: VersionedTransaction) -> VersionedTransaction:
        kp = cls.get_keypair()
        sig = kp.sign_message(to_bytes_versioned(tx.message))
        return VersionedTransaction.populate(tx.message, [sig])

    @classmethod
    def sign_transaction(cls, tx: VersionedTransaction) -> VersionedTransaction:
        """Sign a versioned transaction (alias for :meth:`sign_versioned`)."""
        return cls.sign_versioned(tx)


def get_signer() -> type[HotWalletSigner]:
    """Entrypoint — hot wallet only."""
    HotWalletSigner._enforce_hot_signer_policy()
    return HotWalletSigner


sign_transaction = HotWalletSigner.sign_versioned
