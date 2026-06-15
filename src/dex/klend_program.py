"""KLend anchorpy Program loader — bundled ``idls/klend.json`` (one-time fetch)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

from src.dex.kamino import KAMINO_PROGRAM_ID

logger = logging.getLogger(__name__)

_KLEND_PROGRAM: Any | None = None
_KLEND_IDL_CACHE: dict[str, Any] | None = None

LIQUIDATE_IX_NAME = "liquidateObligationAndRedeemReserveCollateral"
LIQUIDATE_IX_V2_NAME = "liquidateObligationAndRedeemReserveCollateralV2"


def klend_idl_path() -> Path:
    """Resolve KLend IDL path (``KLEND_IDL_PATH`` env or repo ``idls/klend.json``)."""
    override = (os.getenv("KLEND_IDL_PATH") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "idls" / "klend.json"


def load_klend_idl_json() -> dict[str, Any]:
    """Load raw KLend IDL JSON (cached)."""
    global _KLEND_IDL_CACHE
    if _KLEND_IDL_CACHE is not None:
        return _KLEND_IDL_CACHE

    path = klend_idl_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"KLend IDL not found at {path}. Run: npm run fetch:klend-idl"
        )
    _KLEND_IDL_CACHE = json.loads(path.read_text(encoding="utf-8"))
    return _KLEND_IDL_CACHE


def klend_instruction_names() -> set[str]:
    """All instruction names from bundled IDL."""
    idl = load_klend_idl_json()
    return {str(ix.get("name") or "") for ix in idl.get("instructions") or []}


async def get_klend_program(
    connection: AsyncClient,
    keypair: Keypair,
    *,
    force_reload: bool = False,
    ephemeral: bool = False,
) -> Any:
    """
    Load anchorpy ``Program`` from ``idls/klend.json``.

    Use ``ephemeral=True`` for short-lived ``async with AsyncClient(...)`` blocks so
    a closed connection is never stored in the module cache.
    """
    global _KLEND_PROGRAM
    if not ephemeral and _KLEND_PROGRAM is not None and not force_reload:
        return _KLEND_PROGRAM

    try:
        from anchorpy import Idl, Program, Provider, Wallet
    except ImportError as exc:
        raise ImportError(
            "anchorpy is required for KLend Program loading. "
            "Install with: pip install 'anchorpy>=0.20.0,<0.22.0'"
        ) from exc

    idl = Idl.from_json(json.dumps(load_klend_idl_json()))
    provider = Provider(connection, Wallet(keypair))
    program = Program(idl, KAMINO_PROGRAM_ID, provider)
    if not ephemeral:
        _KLEND_PROGRAM = program
        logger.debug("KLend Program loaded from %s", klend_idl_path())
    return program


def reset_klend_program_cache() -> None:
    """Clear cached Program/IDL (tests)."""
    global _KLEND_PROGRAM, _KLEND_IDL_CACHE
    _KLEND_PROGRAM = None
    _KLEND_IDL_CACHE = None
