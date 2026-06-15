"""Backward compatibility — use ``src.cex.backpack.BackpackClient``."""

from src.cex.backpack import BackpackClient, get_backpack_client, get_secret

__all__ = ["BackpackClient", "get_backpack_client", "get_secret"]
