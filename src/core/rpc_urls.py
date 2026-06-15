"""RPC URL resolution — paid/fast primary, public fallback only when explicitly allowed."""

from __future__ import annotations

import os
from typing import Literal

RpcPurpose = Literal["quote", "sim", "send", "balance", "default"]

_PUBLIC_FALLBACK = "https://api.mainnet-beta.solana.com"
_PUBLIC_FALLBACK_ANKR = "https://rpc.ankr.com/solana"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def resolve_rpc_url(purpose: RpcPurpose = "default") -> str:
    """
    Pick RPC endpoint by purpose.

    - ``quote`` / ``sim``: prefer ``SOLANA_RPC_URL_FAST`` (Helius dev tier, etc.)
    - ``send`` / ``balance`` / ``default``: primary ``SOLANA_RPC_URL``
    - Public mainnet-beta only if ``ALLOW_PUBLIC_RPC_FALLBACK=true`` and nothing else set
    """
    chain = resolve_rpc_fallback_chain(purpose)
    return chain[0] if chain else ""


def resolve_rpc_fallback_chain(purpose: RpcPurpose = "default") -> list[str]:
    """Ordered RPC URLs for retries (primary → ``FALLBACK_RPC`` → optional public)."""
    primary = (os.getenv("SOLANA_RPC_URL") or "").strip()
    fast = (os.getenv("SOLANA_RPC_URL_FAST") or "").strip()
    fallback = (os.getenv("FALLBACK_RPC") or "").strip()

    ordered: list[str] = []
    if purpose in ("quote", "sim"):
        for url in (fast, primary):
            if url and url not in ordered:
                ordered.append(url)
    else:
        for url in (primary, fast):
            if url and url not in ordered:
                ordered.append(url)

    if fallback and fallback not in ordered:
        ordered.append(fallback)

    alchemy = (os.getenv("ALCHEMY_RPC") or os.getenv("ALCHEMY_RPC_URL") or "").strip()
    if alchemy and alchemy not in ordered:
        ordered.append(alchemy)

    if _env_bool("ALLOW_PUBLIC_RPC_FALLBACK", False):
        public2 = (os.getenv("RPC_PUBLIC_FALLBACK_2") or "").strip()
        for url in (_PUBLIC_FALLBACK, public2):
            if url and url not in ordered:
                ordered.append(url)

    return ordered


def rpc_provider_label(url: str) -> str:
    """Short label for Prometheus ``provider`` tag."""
    u = (url or "").lower()
    if "helius" in u:
        return "helius"
    if "quicknode" in u or "quiknode" in u:
        return "quicknode"
    if "alchemy" in u:
        return "alchemy"
    if "triton" in u:
        return "triton"
    if "ankr.com" in u:
        return "ankr"
    if "mainnet-beta.solana.com" in u:
        return "public"
    return "custom"
