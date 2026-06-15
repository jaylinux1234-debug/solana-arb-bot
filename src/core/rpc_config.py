"""
Solana RPC resilience — fallback chains, 429 cooldown, WS connect tuning.

Canonical HTTP chain: :func:`resolve_rpc_fallback_chain` in :mod:`src.core.rpc_urls`.
Legacy shim: ``python.rpc_config`` re-exports Base/ETH WS helpers from :mod:`src.core.eth_ws_provider`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from src.core.rpc_urls import (
    resolve_rpc_fallback_chain,
    resolve_rpc_url,
    rpc_provider_label,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RPC_COOLDOWN_UNTIL: dict[str, float] = {}
_BALANCE_CACHE: dict[str, tuple[float, float]] = {}
_UPGRADED_PROVIDER: SolanaRobustProvider | None = None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class SolanaRobustProvider:
    """
    Weighted HTTP RPC chain for Solana (Helius primary/fast + public fallbacks).

    Complements :func:`call_with_rpc_fallback` — not the ETH WS ``RobustMultiProvider``.
    """

    providers: dict[str, str] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    ws_backoff_sec: float = 8.0
    rate_per_sec: float = 15.0
    rate_capacity: float = 30.0

    def http_chain(self, purpose: str = "default") -> list[str]:
        """URLs in priority order (weight-sorted; fast-first for quote/sim)."""
        if not self.providers:
            return resolve_rpc_fallback_chain(purpose)  # type: ignore[arg-type]

        boost: dict[str, float] = {}
        if purpose in ("quote", "sim"):
            boost["helius_fast"] = 0.25

        scored: list[tuple[float, str, str]] = []
        for name, url in self.providers.items():
            if not url:
                continue
            w = float(self.weights.get(name, 0.05)) + boost.get(name, 0.0)
            scored.append((w, name, url))

        scored.sort(key=lambda x: (-x[0], x[1]))
        ordered: list[str] = []
        seen: set[str] = set()
        for _, _, url in scored:
            if url and url not in seen:
                seen.add(url)
                ordered.append(url)
        return ordered


def _rpc_url_from_settings(attr: str) -> str:
    try:
        from src.config.settings import get_settings

        return str(getattr(get_settings(), attr, "") or "").strip()
    except Exception:
        return ""


def get_upgraded_robust_provider(*, force_reload: bool = False) -> SolanaRobustProvider:
    """
    Free-tier multi-provider setup (Helius + optional Alchemy + public fallbacks).

    Env: ``SOLANA_RPC_URL``, ``SOLANA_RPC_URL_FAST``, ``ALCHEMY_RPC``,
    ``ALLOW_PUBLIC_RPC_FALLBACK``, ``RPC_RATE_PER_SEC``, ``RPC_RATE_CAPACITY``.
    """
    global _UPGRADED_PROVIDER
    if (
        _UPGRADED_PROVIDER is not None
        and not force_reload
        and _UPGRADED_PROVIDER.providers
    ):
        return _UPGRADED_PROVIDER

    primary = (os.getenv("SOLANA_RPC_URL") or _rpc_url_from_settings("SOLANA_RPC_URL")).strip()
    fast = (
        os.getenv("SOLANA_RPC_URL_FAST") or _rpc_url_from_settings("SOLANA_RPC_URL_FAST")
    ).strip()
    alchemy = (os.getenv("ALCHEMY_RPC") or os.getenv("ALCHEMY_RPC_URL") or "").strip()
    fallback = (os.getenv("FALLBACK_RPC") or "").strip()
    public1 = "https://api.mainnet-beta.solana.com"
    public2 = (os.getenv("RPC_PUBLIC_FALLBACK_2") or "").strip()

    providers: dict[str, str] = {}
    if primary:
        providers["helius_primary"] = primary
    if fast:
        providers["helius_fast"] = fast
    if alchemy:
        providers["alchemy"] = alchemy
    if fallback:
        providers["custom_fallback"] = fallback
    if _env_bool("ALLOW_PUBLIC_RPC_FALLBACK", False):
        providers["public_fallback1"] = public1
        if public2:
            providers["public_fallback2"] = public2

    weights = {
        "helius_primary": 0.6,
        "helius_fast": 0.3,
        "alchemy": 0.15,
        "custom_fallback": 0.1,
        "public_fallback1": 0.05,
        "public_fallback2": 0.05,
    }

    _UPGRADED_PROVIDER = SolanaRobustProvider(
        providers=providers,
        weights=weights,
        ws_backoff_sec=_env_float("RPC_WS_BACKOFF_SEC", 8.0),
        rate_per_sec=_env_float("RPC_RATE_PER_SEC", 15.0),
        rate_capacity=_env_float("RPC_RATE_CAPACITY", 30.0),
    )
    logger.info(
        "Solana RPC provider | endpoints=%s rate=%.0f/s cap=%.0f",
        list(providers.keys()),
        _UPGRADED_PROVIDER.rate_per_sec,
        _UPGRADED_PROVIDER.rate_capacity,
    )
    return _UPGRADED_PROVIDER


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


_RPC_429_COUNT = 0


def get_rpc_429_count() -> int:
    """Total HTTP 429 / rate-limit events this process lifetime."""
    return _RPC_429_COUNT


def mark_rpc_rate_limited(url: str, *, cooldown_sec: float | None = None) -> None:
    """Deprioritize an endpoint after HTTP 429 / rate-limit for ``cooldown_sec``."""
    global _RPC_429_COUNT
    if not url:
        return
    _RPC_429_COUNT += 1
    sec = cooldown_sec if cooldown_sec is not None else _env_float("RPC_429_COOLDOWN_SEC", 45.0)
    _RPC_COOLDOWN_UNTIL[url] = time.monotonic() + max(5.0, sec)
    try:
        from src.monitoring.metrics import record_rpc_provider_failure

        record_rpc_provider_failure(rpc_provider_label(url))
    except Exception:
        pass
    logger.info(
        "RPC cooldown | provider=%s sec=%.0f",
        rpc_provider_label(url),
        sec,
    )


def is_rpc_in_cooldown(url: str) -> bool:
    until = _RPC_COOLDOWN_UNTIL.get(url)
    if until is None:
        return False
    if time.monotonic() >= until:
        _RPC_COOLDOWN_UNTIL.pop(url, None)
        return False
    return True


def filtered_rpc_fallback_chain(purpose: str = "default") -> list[str]:
    """Weighted multi-provider chain minus endpoints in 429 cooldown."""
    if _env_bool("RPC_USE_UPGRADED_PROVIDER", True):
        urls = get_upgraded_robust_provider().http_chain(purpose)
    else:
        urls = resolve_rpc_fallback_chain(purpose)  # type: ignore[arg-type]
    filtered: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if u and u not in seen and not is_rpc_in_cooldown(u):
            seen.add(u)
            filtered.append(u)
    if filtered:
        return filtered
    # Last resort: legacy chain even if all in cooldown
    return [u for u in resolve_rpc_fallback_chain(purpose) if u]  # type: ignore[arg-type]


def ws_connect_settings() -> tuple[int, float]:
    """``(max_attempts, base_backoff_sec)`` for :class:`src.core.eth_ws_provider.RobustMultiProvider`."""
    return (
        _env_int("WS_CONNECT_MAX_ATTEMPTS", 25),
        _env_float("WS_CONNECT_BASE_BACKOFF_SEC", 12.0),
    )


def is_rate_limited_error(exc: BaseException) -> bool:
    return _rpc_error_detail(exc, ("429", "too many requests", "rate limit"))


def is_rpc_degraded_error(exc: BaseException) -> bool:
    """429/403/5xx — skip endpoint for cooldown and try next in chain."""
    return _rpc_error_detail(
        exc,
        (
            "429",
            "403",
            "401",
            "502",
            "503",
            "504",
            "too many requests",
            "rate limit",
            "forbidden",
            "connection refused",
        ),
    )


def _rpc_error_detail(exc: BaseException, needles: tuple[str, ...]) -> bool:
    detail = str(exc).lower()
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        detail = f"{detail} {cause}".lower()
    return any(n in detail for n in needles)


def cache_balance(key: str, value: float) -> None:
    """Remember a successful balance read (monotonic timestamp)."""
    if value > 0:
        _BALANCE_CACHE[key] = (float(value), time.monotonic())


def get_cached_balance(key: str, max_age_sec: float) -> float | None:
    """Fresh cache hit within ``max_age_sec``."""
    entry = _BALANCE_CACHE.get(key)
    if entry is None:
        return None
    value, cached_at = entry
    if time.monotonic() - cached_at <= max(0.0, max_age_sec):
        return value
    return None


def get_stale_cached_balance(key: str, max_stale_sec: float = 120.0) -> float | None:
    """Last known good balance when live RPC reads fail (e.g. 429 storm)."""
    entry = _BALANCE_CACHE.get(key)
    if entry is None:
        return None
    value, cached_at = entry
    if value > 0 and time.monotonic() - cached_at <= max(5.0, max_stale_sec):
        return value
    return None


async def call_with_rpc_fallback(
    purpose: str,
    fn: Callable[[str], Awaitable[T]],
    *,
    label: str = "rpc",
) -> T:
    """
    Run ``fn(rpc_url)`` across the fallback chain until one succeeds.

    Marks endpoints in cooldown on 429 before trying the next URL.
    """
    urls = filtered_rpc_fallback_chain(purpose)
    if not urls:
        raise RuntimeError(f"{label}: no RPC URLs configured")

    retries = _env_int("RPC_429_RETRY_ATTEMPTS", 3)
    backoff = _env_float("RPC_429_BACKOFF_SEC", 0.5)
    last_exc: Exception | None = None

    for rpc in urls:
        for attempt in range(retries):
            try:
                return await fn(rpc)
            except Exception as exc:
                last_exc = exc
                if is_rpc_degraded_error(exc):
                    mark_rpc_rate_limited(rpc)
                    break
                logger.debug(
                    "%s failed | provider=%s attempt=%s err=%s",
                    label,
                    rpc_provider_label(rpc),
                    attempt + 1,
                    exc,
                )
                break

    raise last_exc or RuntimeError(f"{label}: all RPC endpoints failed")


async def _redis_get_balance(cache_key: str) -> float | None:
    url = (os.getenv("REDIS_URL") or "").strip()
    if not url:
        return None
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(url)
        try:
            raw = await client.get(cache_key)
        finally:
            await client.aclose()
        if raw is None:
            return None
        return float(raw)
    except Exception as exc:
        logger.debug("Redis balance cache read skipped: %s", exc)
        return None


async def _redis_set_balance(
    cache_key: str,
    balance: float,
    *,
    max_age_sec: int,
) -> None:
    url = (os.getenv("REDIS_URL") or "").strip()
    if not url:
        return
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(url)
        try:
            await client.setex(cache_key, max(1, max_age_sec), str(balance))
        finally:
            await client.aclose()
    except Exception as exc:
        logger.debug("Redis balance cache write skipped: %s", exc)


async def get_robust_sol_balance(wallet_pubkey: str | None = None) -> float:
    """Helius 429-resistant on-chain SOL balance (cache + multi-RPC fallback)."""
    from src.core.wallet import get_wallet_pubkey

    pubkey = (wallet_pubkey or get_wallet_pubkey() or "").strip()
    if not pubkey:
        return 0.0

    cache_key = f"sol_balance:{pubkey}"
    ttl = _env_int("SOL_BALANCE_CACHE_SEC", 10)
    stale_ttl = _env_float("SOL_BALANCE_STALE_CACHE_SEC", 120.0)

    redis_cached = await _redis_get_balance(cache_key)
    if redis_cached is not None and redis_cached >= 0:
        cache_balance(cache_key, redis_cached)
        return redis_cached

    mem_cached = get_cached_balance(cache_key, float(ttl))
    if mem_cached is not None:
        return mem_cached

    from solana.rpc.async_api import AsyncClient
    from solders.pubkey import Pubkey

    owner = Pubkey.from_string(pubkey)
    purposes = ["balance", "default", "fast"]
    last_exc: Exception | None = None

    async def _fetch_once(rpc: str) -> float:
        async with AsyncClient(rpc) as client:
            resp = await client.get_balance(owner)
        return int(resp.value or 0) / 1_000_000_000.0

    for purpose in purposes:
        urls = filtered_rpc_fallback_chain(purpose)
        if not urls:
            continue
        for rpc in urls:
            try:
                balance = await _fetch_once(rpc)
                cache_balance(cache_key, balance)
                await _redis_set_balance(cache_key, balance, max_age_sec=ttl)
                return balance
            except Exception as exc:
                last_exc = exc
                if is_rpc_degraded_error(exc):
                    mark_rpc_rate_limited(rpc)
                    logger.warning(
                        "RPC degraded -> switching provider | purpose=%s rpc=%s",
                        purpose,
                        rpc_provider_label(rpc),
                    )
                    continue
                logger.debug(
                    "SOL balance fetch failed | purpose=%s rpc=%s err=%s",
                    purpose,
                    rpc_provider_label(rpc),
                    exc,
                )

    stale = get_stale_cached_balance(cache_key, stale_ttl)
    if stale is not None:
        logger.warning(
            "SOL balance fetch failed — using stale cache %.6f (err=%s)",
            stale,
            last_exc,
        )
        return stale

    logger.error("All SOL balance fetches failed for %s…", pubkey[:12])
    return 0.0


__all__ = [
    "SolanaRobustProvider",
    "cache_balance",
    "call_with_rpc_fallback",
    "filtered_rpc_fallback_chain",
    "get_cached_balance",
    "get_robust_sol_balance",
    "get_stale_cached_balance",
    "get_upgraded_robust_provider",
    "is_rate_limited_error",
    "is_rpc_in_cooldown",
    "mark_rpc_rate_limited",
    "resolve_rpc_fallback_chain",
    "resolve_rpc_url",
    "rpc_provider_label",
    "ws_connect_settings",
]
