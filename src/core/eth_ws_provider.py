# src/core/eth_ws_provider.py — Base / Ethereum WS helpers (monitor tooling)
from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_exponential_jitter,
)
from web3 import AsyncWeb3
from web3.exceptions import ProviderConnectionError

from src.core.rate_limiter import get_rpc_rate_limiter
from src.monitoring.metrics import (
    init_rpc_connection_status_gauges,
    record_rpc_provider_failure,
    set_rpc_connection_status,
)
from src.utils.alerts import schedule_rpc_failure_alert

logger = logging.getLogger(__name__)


def _ws_settings() -> tuple[int, float]:
    try:
        from src.core.rpc_config import ws_connect_settings

        return ws_connect_settings()
    except Exception:
        return (_int_env("WS_CONNECT_MAX_ATTEMPTS", 25), _float_env("WS_CONNECT_BASE_BACKOFF_SEC", 12.0))

T = TypeVar("T")


def _ws_retry_before_sleep(retry_state: Any) -> None:
    exc = None
    if retry_state.outcome is not None and retry_state.outcome.failed:
        exc = retry_state.outcome.exception()
    logger.warning("Retry %s after %s", retry_state.attempt_number, exc)


@retry(
    stop=stop_after_attempt(8),
    wait=wait_exponential_jitter(initial=2, max=30),
    retry=retry_if_exception_type((ConnectionError, asyncio.TimeoutError, TimeoutError, Exception)),
    before_sleep=_ws_retry_before_sleep,
    reraise=True,
)
async def safe_ws_call(coro: Callable[[], Awaitable[T]] | Awaitable[T]) -> T:
    """
    Retry a WebSocket RPC coroutine with exponential jitter.

    Pass a factory (recommended) so each attempt gets a fresh coroutine::

        await safe_ws_call(ws_w3.eth.block_number)
    """
    await get_rpc_rate_limiter().acquire()
    if callable(coro) and not asyncio.iscoroutine(coro):
        return await coro()
    return await coro


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _alchemy_ws_default() -> str | None:
    key = (os.getenv("ALCHEMY_KEY") or "").strip()
    if not key:
        return None
    return f"wss://base-mainnet.g.alchemy.com/v2/{key}"


def _fallback_ws() -> str | None:
    return (os.getenv("FALLBACK_WS") or os.getenv("BASE_WS") or "").strip() or None


def _fallback_http() -> str | None:
    return (os.getenv("BASE_RPC") or "https://mainnet.base.org").strip() or None


def _rpc_provider_mode() -> str:
    return (os.getenv("RPC_PROVIDER") or "quicknode").strip().lower()


def _is_multi_provider_mode() -> bool:
    return _rpc_provider_mode() == "multi"


def _build_providers() -> dict[str, dict[str, Any]]:
    return {
        "quicknode": {
            "http": os.getenv("QUICKNODE_RPC"),
            "ws": os.getenv("QUICKNODE_WS"),
            "weight": 0.65,
            "failures": 0,
        },
        "alchemy": {
            "http": os.getenv("ALCHEMY_RPC"),
            "ws": os.getenv("ALCHEMY_WS") or _alchemy_ws_default(),
            "weight": 0.25,
            "failures": 0,
        },
        "fallback": {
            "http": _fallback_http(),
            "ws": _fallback_ws(),
            "weight": 0.10,
            "failures": 0,
        },
        # Back-compat alias for metrics / legacy code
        "public": {
            "http": _fallback_http(),
            "ws": _fallback_ws(),
            "weight": 0.10,
            "failures": 0,
        },
    }


# Back-compat alias (no per-provider failure counters; skip duplicate "public").
RPC_ENDPOINTS: dict[str, dict[str, Any]] = {
    name: {k: v for k, v in data.items() if k != "failures"}
    for name, data in _build_providers().items()
    if name != "public"
}


class RobustMultiProvider:
    """Production-grade RPC provider with weighted rotation + smart retries."""

    def __init__(self) -> None:
        raw = _build_providers()
        # Dedupe public/fallback — keep single fallback entry for rotation.
        self.providers = {k: v for k, v in raw.items() if k != "public"}
        self.max_failures = 4
        mode = _rpc_provider_mode()
        if _is_multi_provider_mode():
            self.current = self._first_available_provider() or "quicknode"
        elif mode in self.providers:
            self.current = mode
        else:
            self.current = self._first_available_provider() or "quicknode"
        self._max_connect_attempts, self._connect_backoff_sec = _ws_settings()
        self._ws_request_timeout = _int_env("WS_PING_TIMEOUT", 25)
        # Legacy failure map used by monitor_main.record_failure()
        self.failures: dict[str, int] = {name: 0 for name in self.providers}
        init_rpc_connection_status_gauges(list(self.providers.keys()))

    def _first_available_provider(self) -> str | None:
        for name, data in self.providers.items():
            if (data.get("ws") or "").strip():
                return name
        return None

    def _get_weighted_choice(self) -> str:
        """Weighted random selection with failure penalty."""
        choices: list[str] = []
        for name, data in self.providers.items():
            if not data.get("ws"):
                continue
            penalty = 0.2 ** int(data.get("failures", 0))
            weight = int(float(data.get("weight", 0)) * 20 * penalty)
            choices.extend([name] * max(1, weight))
        return random.choice(choices) if choices else "quicknode"

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential_jitter(initial=2, max=30),
        retry=retry_if_exception_type(
            (ProviderConnectionError, ConnectionError, TimeoutError, OSError, ValueError)
        ),
        reraise=True,
    )
    async def _connect_with_jitter(self, provider_name: str) -> AsyncWeb3:
        """Single-provider WS connect (retried with exponential jitter)."""
        config = self.providers[provider_name]
        ws_url = (config.get("ws") or "").strip().rstrip("/")
        if not ws_url:
            raise ValueError(f"No WS URL for {provider_name}")

        w3 = AsyncWeb3(
            AsyncWeb3.WebSocketProvider(
                ws_url,
                websocket_kwargs={"close_timeout": self._ws_request_timeout},
            )
        )
        await w3.provider.connect()
        await w3.eth.block_number
        config["failures"] = 0
        self.failures[provider_name] = 0
        self.current = provider_name
        set_rpc_connection_status(provider_name, True)
        logger.info("Connected to %s WS", provider_name)
        return w3

    async def _connect_ws(self) -> AsyncWeb3:
        """Connect healthy WebSocket client with retries."""
        mode = _rpc_provider_mode()
        for attempt in range(1, self._max_connect_attempts + 1):
            if not _is_multi_provider_mode() and attempt == 1 and mode and mode in self.providers:
                provider_name = mode
            else:
                provider_name = self._get_weighted_choice()
            try:
                return await self._connect_with_jitter(provider_name)
            except Exception as exc:
                config = self.providers[provider_name]
                failures = int(config.get("failures", 0)) + 1
                config["failures"] = failures
                self.failures[provider_name] = failures
                record_rpc_provider_failure(provider_name)
                set_rpc_connection_status(provider_name, False)
                schedule_rpc_failure_alert(exc, provider=provider_name)
                logger.warning(
                    "%s failed (attempt %s): %s",
                    provider_name,
                    attempt,
                    type(exc).__name__,
                )

                if "429" in str(exc):
                    await asyncio.sleep(2**attempt + random.random())

                jitter = self._connect_backoff_sec * (1.0 + random.random())
                await asyncio.sleep(jitter)

        err = ProviderConnectionError("All RPC providers failed after retries")
        set_rpc_connection_status(self.current, False)
        schedule_rpc_failure_alert(err, provider=self.current)
        raise err

    def record_failure(self, name: str | None = None) -> None:
        key = name or self.current
        record_rpc_provider_failure(key)
        set_rpc_connection_status(key, False)
        if key in self.providers:
            self.providers[key]["failures"] = min(
                self.max_failures,
                int(self.providers[key].get("failures", 0)) + 1,
            )
        if key in self.failures:
            self.failures[key] = min(99, self.failures[key] + 1)

    def get_next(self) -> str:
        """Legacy: return WS URL for weighted choice (HTTP helpers)."""
        name = self._get_weighted_choice()
        self.current = name
        ws = (self.providers[name].get("ws") or "").strip()
        if not ws:
            raise ProviderConnectionError(f"WS URL empty for provider {name}")
        return ws

    def get_ws_provider(self) -> _WsSession:
        """Use: ``async with await multi.get_ws_provider() as ws_w3:``"""
        return _WsSession(self)

    def get_ws(self) -> _WsSession:
        """Alias for :meth:`get_ws_provider`."""
        return self.get_ws_provider()


class _WsSession:
    """Async context manager: ``async with await multi.get_ws() as ws_w3:``"""

    def __init__(self, provider: RobustMultiProvider) -> None:
        self._provider = provider
        self._w3: AsyncWeb3 | None = None

    def __await__(self):
        async def _ready() -> _WsSession:
            return self

        return _ready().__await__()

    async def __aenter__(self) -> AsyncWeb3:
        self._w3 = await self._provider._connect_ws()
        return self._w3

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._w3 is not None:
            try:
                prov = self._w3.provider
                if prov is not None and hasattr(prov, "disconnect"):
                    await prov.disconnect()
            except Exception:
                pass
        return False


_multi_provider: RobustMultiProvider | None = None


def get_robust_ws_provider() -> RobustMultiProvider:
    global _multi_provider
    if _multi_provider is None:
        _multi_provider = RobustMultiProvider()
    return _multi_provider


class RobustWSProvider:
    """Single-endpoint WS connect with retries (legacy)."""

    @staticmethod
    @retry(
        stop=stop_after_attempt(8),
        wait=wait_exponential(multiplier=3, min=2, max=30),
        retry=retry_if_exception_type(
            (ProviderConnectionError, ConnectionError, TimeoutError, OSError)
        ),
    )
    async def get(endpoint: str, **kwargs):
        from web3.providers import WebSocketProvider

        provider = WebSocketProvider(endpoint, **kwargs)
        await provider.connect()
        return provider
