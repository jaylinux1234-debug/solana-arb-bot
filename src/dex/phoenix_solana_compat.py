"""Compatibility shim so ``phoenix-trade`` imports work with ``solana>=0.36``."""

from __future__ import annotations

import sys
from types import ModuleType


def ensure_phoenix_import_compat() -> None:
    """
    ``phoenix-trade`` references ``solana.transaction.Instruction`` (removed in solana-py 0.30+).

    Map it to ``solders.instruction.Instruction`` before importing ``phoenix.market``.
    """
    try:
        import solana.transaction  # noqa: F401

        return
    except ModuleNotFoundError:
        pass

    from solders.instruction import Instruction

    transaction_mod = ModuleType("solana.transaction")
    transaction_mod.Instruction = Instruction
    sys.modules["solana.transaction"] = transaction_mod

    import solana

    solana.transaction = transaction_mod  # type: ignore[attr-defined]
