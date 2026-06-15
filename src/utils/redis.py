"""Optional Redis for cycle snapshots and cross-process state."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_client: Any = None


async def get_redis():
    """Return async Redis client or None if REDIS_URL unset / redis package missing."""
    global _client
    url = (os.getenv("REDIS_URL") or "").strip()
    if not url:
        return None
    if _client is not None:
        return _client
    try:
        import redis.asyncio as redis
    except ImportError:
        logger.warning("redis package not installed — pip install redis")
        return None

    _client = redis.from_url(url, decode_responses=True)
    return _client


async def ping_redis() -> bool:
    r = await get_redis()
    if r is None:
        return False
    try:
        return bool(await r.ping())
    except Exception as exc:
        logger.warning("Redis ping failed: %s", exc)
        return False


async def close_redis() -> None:
    global _client
    if _client is None:
        return
    try:
        await _client.aclose()
    except Exception:
        pass
    _client = None


async def cache_json(key: str, payload: dict[str, Any], ttl_sec: int = 300) -> None:
    r = await get_redis()
    if r is None:
        return
    try:
        await r.setex(key, ttl_sec, json.dumps(payload))
    except Exception as exc:
        logger.debug("Redis cache_json failed: %s", exc)


# Alias for imports: ``from src.utils.redis import redis_client``
redis_client = get_redis


async def get_cached_json(key: str) -> dict[str, Any] | None:
    r = await get_redis()
    if r is None:
        return None
    try:
        raw = await r.get(key)
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("Redis get_cached_json failed: %s", exc)
        return None
